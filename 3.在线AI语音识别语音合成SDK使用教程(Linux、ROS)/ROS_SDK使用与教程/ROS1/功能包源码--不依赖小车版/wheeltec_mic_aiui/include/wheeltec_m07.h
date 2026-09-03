#ifndef WHEELTEC_M07_H
#define WHEELTEC_M07_H

#include <ros/ros.h>
#include <std_msgs/Int8.h>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <cstring>
#include <iostream>
#include <string>

// ================ 配置参数结构体 ================
struct WheeltecConfig {
    std::string port = "/dev/wheeltec_mic";
    int baud = 115200;
    bool debug = false;         // 调试模式
    double min_interval = 0.3;  // 最小唤醒间隔(秒)
};

// ================ 串口管理类 ================
class SerialPort {
private:
    int fd;
    std::string port_name;
    
public:
    SerialPort(const std::string& port);
    ~SerialPort();
    
    bool open(int baud);
    int read_data(uint8_t* buf, size_t size);
    void close();
};

// ================ 唤醒检测器类 ================
class AwakeDetector {
private:
    ros::Publisher pub_awake;
    ros::Publisher pub_voice;
    WheeltecConfig config;
    SerialPort serial;
    
    // 状态机
    enum State { IDLE, GOT_DE, GOT_5B } state;
    
    // 时间控制
    ros::Time last_awake_time;
    
    void publish_voice_flag();
    void process_bytes(uint8_t* data, int len);
    void check_awake_packet(uint8_t third_byte);
    
public:
    AwakeDetector(ros::NodeHandle& nh, const WheeltecConfig& cfg);
    ~AwakeDetector() = default;
    
    void process();  // 主处理函数
};

#endif // WHEELTEC_M07_H