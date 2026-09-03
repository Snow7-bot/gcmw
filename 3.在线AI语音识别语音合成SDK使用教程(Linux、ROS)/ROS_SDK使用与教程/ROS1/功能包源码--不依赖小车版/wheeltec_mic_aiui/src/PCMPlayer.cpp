#include "PCMPlayer.h"

PCMPlayer::PCMPlayer(unsigned int rate = 16000, 
         snd_pcm_format_t fmt = SND_PCM_FORMAT_S16_LE, 
         int ch = 1)
    : sample_rate(rate), format(fmt), channels(ch), playback_handle(nullptr){}

PCMPlayer::~PCMPlayer() {
    std::lock_guard<std::mutex> lock(alsa_mutex);
    if (playback_handle) {
        snd_pcm_drain(playback_handle);
        snd_pcm_close(playback_handle);
    }
}

/**
 * 初始化播放器
 */
bool PCMPlayer::init_alsa() {
    int err;
    unsigned int buffer_time, period_time; 
    if (err = snd_pcm_open(&playback_handle, DEVICE_NAME, SND_PCM_STREAM_PLAYBACK, 0)) {
        std::cerr << "ALSA open error: " << snd_strerror(err) << std::endl;
        return false;
    }

    snd_pcm_hw_params_t* params;
    snd_pcm_hw_params_alloca(&params);
    snd_pcm_hw_params_any(playback_handle, params);

    if ((err = snd_pcm_hw_params_set_access(playback_handle, params, 
                                          SND_PCM_ACCESS_RW_INTERLEAVED))) {
        std::cerr << "Set access error: " << snd_strerror(err) << std::endl;
        return false;
    }

    // 设置音频参数（需要与输入PCM数据匹配）
    snd_pcm_hw_params_set_format(playback_handle, params, format);
    snd_pcm_hw_params_set_channels(playback_handle, params, channels);
    snd_pcm_hw_params_set_rate_near(playback_handle, params, &sample_rate, 0);

    // 设置周期大小（
    snd_pcm_uframes_t period_size = 640;
    snd_pcm_hw_params_set_period_size_near(playback_handle, params, &period_size, NULL);

    // 设置缓冲区大小（周期大小的 4 倍）
    snd_pcm_uframes_t buffer_size = period_size * 4;
    snd_pcm_hw_params_set_buffer_size_near(playback_handle, params, &buffer_size);

    if ((err = snd_pcm_hw_params(playback_handle, params))) {
        std::cerr << "Params set error: " << snd_strerror(err) << std::endl;
        return false;
    }

    // 软件参数优化（减少启动欠载）
    snd_pcm_sw_params_t* sw_params;
    snd_pcm_sw_params_alloca(&sw_params);
    snd_pcm_sw_params_current(playback_handle, sw_params);
    snd_pcm_sw_params_set_start_threshold(playback_handle, sw_params, buffer_size/2);
    snd_pcm_sw_params_set_stop_threshold(playback_handle, sw_params, buffer_size);
    snd_pcm_sw_params_set_avail_min(playback_handle, sw_params, period_size);
    snd_pcm_sw_params(playback_handle, sw_params);

    return true;
}

/**
 * 设备重连
 */
bool PCMPlayer::reconnect() {
    if (playback_handle) {
        snd_pcm_drop(playback_handle);
        snd_pcm_close(playback_handle);
        playback_handle = nullptr;
    }
    sleep(1); 
    return init_alsa();
}

/**
 * 准备PCM音频播放设备
 */
void PCMPlayer::prepare() {
    std::lock_guard<std::mutex> lock(alsa_mutex);
    if (!playback_handle) {
        std::cerr << "Playback device not initialized." << std::endl;
        return;
    }

    // 检查设备状态并恢复
    snd_pcm_state_t state = snd_pcm_state(playback_handle);
    if (state == SND_PCM_STATE_XRUN) {
        snd_pcm_recover(playback_handle, -EPIPE, 1);
    } else if (state != SND_PCM_STATE_RUNNING && state != SND_PCM_STATE_PREPARED) {
        snd_pcm_drop(playback_handle); 
    } else {
        int err = snd_pcm_drain(playback_handle);
        if (err < 0) {
            std::cerr << "Error draining PCM device: " << snd_strerror(err) << std::endl;
            snd_pcm_drop(playback_handle);
        }
    }

    int err = snd_pcm_prepare(playback_handle);
    if (err < 0) {    
        if (reconnect()) {
            err = snd_pcm_prepare(playback_handle);
            if (err < 0) {
                std::cerr << "Still failed to prepare after reconnection: " << snd_strerror(err) << std::endl;
                return;
            }
        } else {
            std::cerr << "Reconnection failed." << std::endl;
            return;
        }
    }
}

/**
 * 播放pcm音频段数据
 */
void PCMPlayer::play_pcm(const char* audio_data, int len, int dts) {
    std::lock_guard<std::mutex> lock(alsa_mutex);
    
    if (!playback_handle) {
        std::cerr << "Playback handle is null, attempting to reconnect..." << std::endl;
        if (!reconnect()) {
            std::cerr << "Failed to reconnect, cannot play audio." << std::endl;
            return;
        }
    }

    switch (dts) {
        case 0: // 音频开始
            write_audio(audio_data, len);
            break;
            
        case 1: // 音频中间块
            write_audio(audio_data, len);
            break;
            
        case 2: // 音频结束
            write_audio(audio_data, len);
            break;
            
        case 3: // 独立音频,合成短文本时出现
            write_audio(audio_data, len);
            break;
    }
}

/**
 * 写入播放数据
 */
void PCMPlayer::write_audio(const char* data, int len) {
    if (!playback_handle) return;
    snd_pcm_uframes_t frames = len / (channels * snd_pcm_format_width(format)/8);
    int err;
    
    if ((err = snd_pcm_writei(playback_handle, data, frames)) < 0) {
        //std::cerr << "Write error: " << snd_strerror(err) << std::endl;
        if (err == -EPIPE) {
            std::cerr << "Underrun occurred, recovering..." << std::endl;
            if (snd_pcm_recover(playback_handle, err, 0) < 0) {
                std::cerr << "Recovery failed, preparing device..." << std::endl;
                snd_pcm_prepare(playback_handle);
            } else {
                std::cout << "Recovery Success." << std::endl;
            }
        } else if (err == -ESTRPIPE) {
            std::cerr << "Stream suspended, resuming..." << std::endl;
            while ((err = snd_pcm_resume(playback_handle)) == -EAGAIN) {
                usleep(1000); 
            }
            if (err < 0) {
                std::cerr << "Resume failed, preparing device..." << std::endl;
                snd_pcm_prepare(playback_handle);
            }
        } else { 
            std::cerr << "Unknown error, preparing device..." << std::endl;
            if (reconnect()) {
                std::cout << "PCMPlayer: 重连成功，重新尝试写入数据" << std::endl;
                if ((err = snd_pcm_writei(playback_handle, data, frames)) < 0) {
                    std::cerr << "重连后仍然写入失败: " << snd_strerror(err) << std::endl;
                }
            } else {
                std::cerr << "PCMPlayer: 重连失败，无法继续播放" << std::endl;
            }
        }
    }

}
