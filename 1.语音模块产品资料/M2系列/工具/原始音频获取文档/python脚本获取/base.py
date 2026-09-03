#!/usr/bin/env python
# -*- coding: utf-8 -*-
import struct
import serial
import time
import sys
import re
import os
import subprocess
from pathlib import Path

# 串口交互字段说明
record_set = '{"type":"dump_audio","content":{"debug":3}}'                  # 0:停止记录音频/3:开始记录音频(原始和降噪音频)
clean_set = '{"type":"clean_pcm"}'                                          # 清空音频
version_check = '{"type": "version"}'                                       # 固件版本信息
awake_word_set = '{"keyword": "xiao3 wei1 xiao3 wei1","threshold": "900"}'  # 唤醒词设置
manual_wakeup_set = '{"type": "manual_wakeup","content": {"beam": 1}}'      # 手动唤醒设置波束0~5
mic6_set = '{"type":"switch_mic","content":{"mic":"mic6"}}'				# 线性六麦设置
mic_circle_set = '{"type":"switch_mic","content":{"mic":"mic6_circle"}}'			# 环形六麦设置

class MicArrayController:
    def __init__(self, port, baudrate):
        # 初始化串口连接
        self.ser = serial.Serial(port, baudrate)
        # 音频文件保存路径
        self.save_path = '/mnt/usb/audio/'
        # 测试音频文件路径
        self.audio_test = 'config/test.wav'
        # 降噪音频文件路径
        self.iat_pcm = '/data/build/iat.pcm'
        # 原始音频文件路径
        self.origin_pcm = '/data/build/origin.pcm'
        # 运行状态标志
        self.running = True
        # 打印连接状态
        print("M2模块已连接" if self.ser.isOpen() else "连接M2模块失败")

    # M2设备消息封装
    def Msg_Packet(self, Content):
        # 消息头定义
        Sync_Head = 0xA5
        User_ID = 0x01
        Msg_Type = 0x05
        MsgId_L = 0x01
        MsgId_H = 0x00
        # 计算消息长度
        MsgLen_Byte = len(Content).to_bytes(2, 'big')
        Msg_Len_L = MsgLen_Byte[1]
        Msg_Len_H = MsgLen_Byte[0]
        # 计算校验码
        CheckCode = ((~sum([Sync_Head, User_ID, Msg_Type, Msg_Len_L, Msg_Len_H, MsgId_L, MsgId_H] + list(Content))) & 0xFF) +1

        # 消息封装 - 将各字段转换为字节
        Sync_Head_ = Sync_Head.to_bytes(1, 'big')
        User_ID_ = User_ID.to_bytes(1, 'big')
        Msg_Type_ = Msg_Type.to_bytes(1, 'big')
        Msg_Len_L_ = Msg_Len_L.to_bytes(1, 'big')
        Msg_Len_H_ = Msg_Len_H.to_bytes(1, 'big')
        MsgId_L_ = MsgId_L.to_bytes(1, 'big')
        MsgId_H_ = MsgId_H.to_bytes(1, 'big')
        CheckCode_ = CheckCode.to_bytes(1, 'big')
        # 组合完整消息
        Msg = Sync_Head_ + User_ID_ + Msg_Type_ + Msg_Len_L_ + Msg_Len_H_ + MsgId_L_ + MsgId_H_ + Content + CheckCode_
        return Msg

    # 类型切换 - 替换字符串中的数字
    def type_chage(self, string, num):
        return re.sub(r'\d+', str(num), string)
   
    # 录音模式设置 
    def record_mode(self, status):
        if status == 0:
            # 停止录音
            Content = self.type_chage(record_set, status)
            self.ser.write(self.Msg_Packet(Content.encode()))
        elif status == 3:
            # 开始录音前先清空音频，然后开始录音
            self.ser.write(self.Msg_Packet(clean_set.encode()))
            Content = self.type_chage(record_set, status)
            self.ser.write(self.Msg_Packet(Content.encode()))
        else:
            print("Unset mode!")

    # 版本查询
    def version_check(self):
        self.ser.write(self.Msg_Packet(version_check.encode()))

    # 麦克风类型切换
    def mic_type(self, type_num):
        if type_num == 0:
           # 切换到线性六麦
           self.ser.write(self.Msg_Packet(mic6_set.encode()))
        elif type_num == 1:
           # 切换到环形六麦
           self.ser.write(self.Msg_Packet(mic_circle_set.encode()))
        else:
           print("Type Set ERROR")

    # 获取pcm音频
    def get_pcm(self, position):
        # 创建保存目录
        Path(self.save_path).mkdir(parents=True, exist_ok=True)
        try:
            # 拉取原始音频文件
            origin_name = f"origin_{position}°.pcm"
            subprocess.run(["adb", "pull", self.origin_pcm, os.path.join(self.save_path, origin_name)], check=True)
            # 拉取降噪音频文件
            iat_name = f"iat_{position}°.pcm"
            subprocess.run(["adb", "pull", self.iat_pcm, os.path.join(self.save_path, iat_name)], check=True)
            print("音频文件拉取成功")
        except subprocess.CalledProcessError as e:
            print(f"命令执行错误: {e}")
    
    # 删除音频文件
    def delete_audio(self, Path=None):
        # 使用指定路径或默认路径
        file_path = Path or self.save_path
        if not os.path.exists(file_path):
            print(f"文件夹 '{file_path}' 不存在。")
            return
        
        # 遍历文件夹删除所有.pcm文件
        for entry in os.scandir(file_path):  
            if entry.is_file() and entry.name.lower().endswith('.pcm'):
                try:
                    os.remove(entry.path)
                    print(f"已删除文件: {entry.path}")
                except Exception as e:
                    print(f"删除文件 {entry.path} 时出错: {e}")
    
    # 播放测试音频
    def play_audio(self, file_path=None):
        # 使用指定文件或默认测试文件
        file_path = file_path or self.audio_test
        subprocess.run(["aplay", "-D", "plughw:CARD=Device,DEV=0", file_path], check=True)

    # 设置波束
    def set_beam(self, beam):
        # 设置指定的波束方向
        Content = self.type_chage(manual_wakeup_set, beam)
        self.ser.write(self.Msg_Packet(Content.encode()))
        print(f"切换到波束{beam}")
        return
    
    # 串口反馈处理
    def feedback(self):
        while self.running:
            try:
                # 检查消息头
                if self.ser.read(1).hex() == 'a5':
                    user_id = self.ser.read(1).hex()
                    msg_type = self.ser.read(1).hex()
                    # 读取消息长度
                    msg_len = int.from_bytes(self.ser.read(2), 'little')
                    msg_id = self.ser.read(2).hex()
                    # 读取数据内容
                    data = self.ser.read(msg_len)
                    checksum = self.ser.read(1).hex()
                    # 处理版本信息响应
                    if msg_type == '04':
                        print(data.decode('utf-8'))
                        break
            except Exception as e:
                print(f"数据解析异常: {str(e)}")
                # 清空输入缓冲区
                self.ser.reset_input_buffer()

def main():
    # 创建麦克风阵列控制器实例
    mic = MicArrayController('/dev/wheeltec_mic', 115200)

    # 固件版本查询
    mic.version_check()
    mic.feedback()

    # 麦克风类型切换（0：线性六麦 1：环形六麦）
    #mic.mic_type(0)
    #mic.feedback()

    # 麦克风波束设置
    #mic.set_beam(0)
    #mic.feedback()

    # 停止运行
    mic.running = False
    print("程序已终止")

if __name__ == "__main__":
    main()