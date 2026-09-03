/****************************************************************/
/* Copyright (c) 2025 WHEELTEC Technology, Inc                */
/* 功能：唤醒检测器实现文件                                       */
/****************************************************************/

#include "wheeltec_m07.h"

// ================ SerialPort 实现 ================
SerialPort::SerialPort(const std::string& port) : fd(-1), port_name(port) {}

SerialPort::~SerialPort() {
    close();
}

bool SerialPort::open(int baud) {
    fd = ::open(port_name.c_str(), O_RDWR | O_NOCTTY);
    if (fd < 0) {
        std::cerr << "错误: 无法打开串口 " << port_name << std::endl;
        return false;
    }
    
    // 配置串口
    struct termios tty;
    tcgetattr(fd, &tty);
    
    // 设置波特率
    speed_t speed = B115200;
    switch(baud) {
        case 9600: speed = B9600; break;
        case 19200: speed = B19200; break;
        case 38400: speed = B38400; break;
        case 57600: speed = B57600; break;
        case 115200: speed = B115200; break;
    }
    cfsetispeed(&tty, speed);
    cfsetospeed(&tty, speed);
    
    // 8N1配置
    tty.c_cflag &= ~PARENB;
    tty.c_cflag &= ~CSTOPB;
    tty.c_cflag &= ~CSIZE;
    tty.c_cflag |= CS8;
    tty.c_cflag &= ~CRTSCTS;
    tty.c_cflag |= CREAD | CLOCAL;
    
    tty.c_iflag &= ~(IXON | IXOFF | IXANY);
    tty.c_lflag &= ~(ICANON | ECHO | ECHOE | ISIG);
    tty.c_oflag &= ~OPOST;
    
    tty.c_cc[VMIN] = 0;
    tty.c_cc[VTIME] = 1;
    
    tcsetattr(fd, TCSANOW, &tty);
    tcflush(fd, TCIOFLUSH);
    
    std::cout << ">>>>>串口打开成功: " << port_name << " @ " << baud << "bps" << std::endl;
    return true;
}

int SerialPort::read_data(uint8_t* buf, size_t size) {
    return read(fd, buf, size);
}

void SerialPort::close() {
    if (fd >= 0) ::close(fd);
    fd = -1;
}

// ================ AwakeDetector 实现 ================
AwakeDetector::AwakeDetector(ros::NodeHandle& nh, const WheeltecConfig& cfg) 
    : config(cfg), serial(cfg.port), state(IDLE) {
    
    pub_awake = nh.advertise<std_msgs::Int8>("/awake_flag", 10);
    pub_voice = nh.advertise<std_msgs::Int8>("/voice_flag", 10);
    
    if (!serial.open(config.baud)) {
        throw std::runtime_error("串口初始化失败");
    }
    publish_voice_flag();
}

void AwakeDetector::process() {
    uint8_t buf[256];
    int n = serial.read_data(buf, sizeof(buf));
    
    if (n > 0) {
        process_bytes(buf, n);
    }
}

void AwakeDetector::publish_voice_flag() {
    std_msgs::Int8 voice_msg;
    voice_msg.data = 1;
    pub_voice.publish(voice_msg);
    std::cout << ">>>>>成功打开麦克风设备" << std::endl;
    std::cout << ">>>>>以降噪板设置的唤醒词为准[默认:你好小微] " << std::endl;
}

void AwakeDetector::process_bytes(uint8_t* data, int len) {
    for (int i = 0; i < len; i++) {
        uint8_t byte = data[i];
        
        switch(state) {
            case IDLE:
                if (byte == 0xDE) state = GOT_DE;
                break;
                
            case GOT_DE:
                if (byte == 0x5B) {
                    state = GOT_5B;
                } else {
                    state = IDLE;
                }
                break;
                
            case GOT_5B:
                // 检查是否完整唤醒包
                check_awake_packet(byte);
                state = IDLE;
                break;
        }
        
        if (config.debug && len > 0) {
            static int count = 0;
            if (count++ % 50 == 0) {
                std::cout << "数据: ";
                for (int j = 0; j < std::min(len, 10); j++) {
                    printf("%02X ", data[j]);
                }
                std::cout << std::endl;
            }
        }
    }
}

void AwakeDetector::check_awake_packet(uint8_t third_byte) {
    ros::Time now = ros::Time::now();
    double interval = (now - last_awake_time).toSec();
    
    if (interval > config.min_interval) {
        // 发布awake_flag
        std_msgs::Int8 awake_msg;
        awake_msg.data = 1;
        pub_awake.publish(awake_msg);
        last_awake_time = now;
        std::cout << ">>>>>已成功唤醒！" << std::endl;
    }
}

int main(int argc, char** argv) {
    ros::init(argc, argv, "awake_detector");
    ros::NodeHandle nh("~");
    
    // 读取配置
    WheeltecConfig cfg;
    nh.param("usart_port_name", cfg.port, cfg.port);
    nh.param("serial_baud_rate", cfg.baud, cfg.baud);
    nh.param("debug_mode", cfg.debug, cfg.debug);
    nh.param("min_interval", cfg.min_interval, cfg.min_interval);
    
    try {
        // 直接创建实例并运行
        AwakeDetector detector(nh, cfg);
        ros::Rate rate(100);
        
        while (ros::ok()) {
            detector.process();
            ros::spinOnce();
            rate.sleep();
        }
    }
    catch (const std::exception& e) {
        std::cerr << "错误: " << e.what() << std::endl;
        return 1;
    }
    
    return 0;
}