/****************************************************************/
/* Copyright (c) 2025 WHEELTEC Technology, Inc                  */
/* function:Serial port analysis                                */
/* 功能：串口解析                                                  */
/****************************************************************/
#include "wheeltec_mic.h"

/**************************************
Function: the protocol parser
功能: 协议解析器
***************************************/
struct ProtocolParser {
    ParseState state = ParseState::WAIT_HEADER;
    std::vector<uint8_t> buffer;
    uint16_t expected_length = 0;
    uint16_t message_id = 0;
    uint8_t message_type = 0;

    void reset() {
        state = ParseState::WAIT_HEADER;
        buffer.reserve(MAX_FRAME_SIZE); 
        buffer.clear();
        expected_length = 0;
        message_id = 0;
        message_type = 0;
    }
};

/**************************************
Function: Parse JSON data to extract angle information
功能: 解析JSON数据以提取角度信息
***************************************/
bool parse_from_json(const std::string& json_str, int& angle) {
    Json::Value root;
    Json::CharReaderBuilder reader;
    std::unique_ptr<Json::CharReader> json_reader(reader.newCharReader());
    std::string errors;

    if (!json_reader->parse(json_str.c_str(), json_str.c_str() + json_str.size(), &root, &errors)) {
        //ROS_WARN("Outer JSON parse error: %s", errors.c_str());
        return false;
    }

    if (!root.isMember("content") || !root["content"].isObject()) {
        //ROS_WARN("Missing 'content' object in outer JSON");
        return false;
    }

    const Json::Value& content = root["content"];

    if (!content.isMember("info") || !content["info"].isString()) {
        //ROS_WARN("Missing or invalid 'info' field");
        return false;
    }
    std::string inner_json_str = content["info"].asString();

    Json::Value inner_root;
    if (!json_reader->parse(inner_json_str.c_str(), inner_json_str.c_str() + inner_json_str.size(), &inner_root, &errors)) {
        //ROS_WARN("Inner JSON parse error: %s", errors.c_str());
        return false;
    }

    if (!inner_root.isMember("ivw") || 
        !inner_root["ivw"].isObject() || 
        !inner_root["ivw"].isMember("angle") || 
        !inner_root["ivw"]["angle"].isNumeric()) {
        //ROS_WARN("Missing or invalid 'ivw.angle' field");
        return false;
    }

    angle = inner_root["ivw"]["angle"].asInt();
    return true;
}

/**************************************
Function: Parse little-endian bytes to uint16
功能: 解析小端字节序为16位无符号整数
***************************************/
uint16_t parse_little_endian(const uint8_t* data) {
    return (data[1] << 8) | data[0];
}

/**************************************
Function: Calculate checksum for data verification
功能: 计算数据校验和用于验证
***************************************/
uint8_t calculate_checksum(const std::vector<uint8_t>& data) {
    uint8_t checksum = 0;
    for (size_t i = 0; i < data.size() - 1; ++i) {
        checksum += data[i];
    }
    return static_cast<uint8_t>(~checksum + 1);
}

/**************************************
Function: Initialize and open serial port with retry mechanism
功能: 串口资源管理类 
***************************************/
SerialPort::SerialPort(const char* port, int baud) : fd(-1) {
    const int max_retries = 5;  // 增加最大重试次数到5次
    int retry_count = 0;
    while (ros::ok() && retry_count++ < max_retries) {
        fd = open(port, O_RDWR | O_NOCTTY);
        if (fd >= 0) {
            // 清除O_NONBLOCK标志，改为阻塞模式
            int flags = fcntl(fd, F_GETFL, 0);
            fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);
            
            if (configure(baud)) {  // 配置成功
                printf(">>>>>成功打开麦克风设备\n");
                printf(">>>>>唤醒词:\"%s!\"\n", awake_words_str.c_str());
                for (int i = 0; i < 2; ++i){
                    std_msgs::Int8 voice_flag_msg;
                    voice_flag_msg.data = 1;
                    voice_flag_pub.publish(voice_flag_msg);
                    //printf(">>>>>voice_flag_msg:%d\n", voice_flag_msg.data);
                    sleep(1.0);
                }
                check_connection(); 
                return;
            }
            close(fd);
        }
        printf("打开串口 %s 失败，重试中 (%d/%d)...\n", port_name.c_str(), retry_count, max_retries);
        printf(">>>>>无法打开麦克风设备，尝试重新连接进行测试\n");
        ros::Duration(1.0).sleep();
    }

    if (!ros::ok()) {
        throw std::runtime_error("Serial port opening interrupted by ROS shutdown");
    } else {
        throw std::runtime_error("Open serial port failed after maximum retries");
    }
}

/**************************************
Function: Close serial port when object is destroyed
功能: 对象销毁时关闭串口
***************************************/
SerialPort::~SerialPort() {
    if (fd >= 0) {
        close(fd);
        ROS_INFO("Serial port closed");
    }
}

/**************************************
Function: Check if serial port connection is still valid
功能: 检查串口连接是否仍然有效
***************************************/
bool SerialPort::check_connection() {
        uint8_t dummy;
        int ret = read(fd, &dummy, 1);
        if (ret == -1 && errno == EAGAIN) {
            // 设备无数据但连接正常
            return true;
        }
        return ret >= 0;
    }

/**************************************
Function: Read data from serial port in non-blocking mode
功能: 以非阻塞模式从串口读取数据
***************************************/
ssize_t SerialPort::read_nonblock(unsigned char* buf, size_t size) {
    struct pollfd fds = {fd, POLLIN, 0};
    int ret = poll(&fds, 1, 10); // 10ms超时
    if (ret > 0 && (fds.revents & POLLIN)) {
        return read(fd, buf, size);
    }
    return -1;
}

/**************************************
Function: Configure serial port parameters
功能: 配置串口参数
***************************************/
bool SerialPort::configure(int baud) {
    struct termios tio;
    tcgetattr(fd, &tio);

    cfmakeraw(&tio);
    cfsetspeed(&tio, baud);

    tio.c_cflag &= ~PARENB;   // 无奇偶校验
    tio.c_cflag &= ~CSTOPB;   // 1位停止位
    tio.c_cflag &= ~CSIZE;
    tio.c_cflag |= CS8;       // 8位数据位

    tio.c_cc[VTIME] = 0;     // 非规范模式读取超时
    tio.c_cc[VMIN] = 0;

    if (tcsetattr(fd, TCSANOW, &tio) != 0) {
        throw std::runtime_error("Failed to configure serial port");
    }
    return tcsetattr(fd, TCSANOW, &tio) == 0;
}

/**************************************
Function: Write data to serial port with retry mechanism
功能: 使用重试机制向串口写入数据
***************************************/
bool EnhancedSerial::reliable_write(const uint8_t* data, size_t len, int retries=3) {
    while(retries-- > 0) {
        ssize_t written = write(fd, data, len);
        if(written == static_cast<ssize_t>(len)) return true;
        usleep(100000); // 100ms重试间隔
    }
    return false;
}

/**************************************
Function: Initialize microphone processor with ROS publishers
功能: 使用ROS发布器初始化麦克风处理器
***************************************/
MicProcessor::MicProcessor(ros::NodeHandle& nh) : nh(nh) {
    awake_flag_pub = nh.advertise<std_msgs::Int8>("/awake_flag", 1);
    voice_words_pub = nh.advertise<std_msgs::String>("/voice_words", 1);
    pub_awake_angle = nh.advertise<std_msgs::Int32>("/mic/awake/angle", 1);
}

/**************************************
Function: Process wake-up event and publish related messages
功能: 处理唤醒事件并发布相关消息
***************************************/
void MicProcessor::process_awake_event(int angle) {
    std_msgs::Int32 angle_msg;
    angle_msg.data = angle;
    pub_awake_angle.publish(angle_msg);

    std_msgs::Int8 flag_msg;
    flag_msg.data = 1;
    awake_flag_pub.publish(flag_msg);

    // std_msgs::String voice_msg;
    // voice_msg.data = "小车唤醒";
    // voice_words_pub.publish(voice_msg);
}

/**************************************
Function: Process valid protocol frame based on message type
功能: 根据消息类型处理有效的协议帧
***************************************/
void process_valid_frame(const std::vector<uint8_t>& frame,
                        uint8_t message_type, 
                        uint16_t message_id,
                        MicProcessor& processor) {
    switch (message_type) {
        case 0x01: // 握手请求
            //ROS_INFO("Handshake request received, ID: %d", message_id);
            break;
        case 0x04:{ // 设备消息
            //ROS_INFO("Device message received, ID: %d", message_id);
            // 提取JSON数据
            uint16_t data_length = (frame[3] << 8) | frame[4];
            const uint8_t* json_start = frame.data() + 7;
            std::string json_str(json_start, json_start + data_length);

            // 解析JSON
            int angle;
            if (!parse_from_json(json_str, angle)) {
                //ROS_WARN("Failed to parse angle from JSON");
                break;
            }
            processor.process_awake_event(angle);
            std::cout << ">>>>>唤醒角度为: " << angle << "°"<< std::endl;
            break;
        }
        case 0x05: // 主控消息
            ROS_INFO("Control message received, ID: %d", message_id);
            break;
        case 0xFF: // 确认消息
            ROS_INFO("Ack message received, ID: %d", message_id);
            break;
        default:
            ROS_WARN("Unknown message type: 0x%02X", message_type);
            break;
    }
}

/**************************************
Function: Main program entry point for microphone control
功能: 麦克风控制的主程序入口点
***************************************/
/**************************************
Function: Build binary protocol packet for M2 serial
功能: 构建发送给M2的二进制协议包
***************************************/
std::string MakeMsgPacket(unsigned short sid, unsigned char type, const std::string &content)
{
    const unsigned short size = content.size();
    std::string data;
    data += (char)FRAME_HEADER;
    data += (char)USER_ID;
    data += (char)type;
    data += (char)(size & 0xff);
    data += (char)((size >> 8) & 0xff);
    data += (char)(sid & 0xff);
    data += (char)((sid >> 8) & 0xff);
    data += content;
    int sum = std::accumulate(data.cbegin(), data.cend(), 0);
    data += (char)((~sum + 1) & 0xff);
    return data;
}

/**************************************
Function: ROS callback for manual wakeup trigger
功能: 接收前端唤醒触发，通过串口发送指令给M2
***************************************/
void wakeup_trigger_callback(const std_msgs::String& msg)
{
    ROS_INFO(">>>>>收到前端唤醒触发: %s", msg.data.c_str());
    if (g_serial == nullptr) {
        ROS_ERROR(">>>>>串口未初始化，无法发送唤醒指令");
        return;
    }
    /* 构建 manual_wakeup JSON */
    std::string json = "{\"type\":\"manual_wakeup\",\"content\":{\"beam\":0}}";
    /* 构建二进制协议包 (type=0x05 = CONTROL) */
    unsigned short sid = (unsigned short)(ros::Time::now().toNSec() & 0xFFFF);
    std::string packet = MakeMsgPacket(sid, 0x05, json);
    /* 发送 */
    if (g_serial->reliable_write((const uint8_t*)packet.data(), packet.size())) {
        ROS_INFO(">>>>>已发送手动唤醒指令给M2");
    } else {
        ROS_ERROR(">>>>>发送手动唤醒指令失败");
    }
}

int main(int argc, char** argv) {
    ros::init(argc, argv, "wheeltec_mic");
    ros::NodeHandle nh("~");
    ros::Rate loop_rate(100);

    voice_flag_pub = nh.advertise<std_msgs::Int8>("/voice_flag", 1);
    nh.param("usart_port_name", port_name, std::string("/dev/wheeltec_mic"));

    ProtocolParser parser;
    MicProcessor mic_processor(nh);
    
    while(ros::ok()) {
        try {
            // 初始化硬件接口
            EnhancedSerial serial(port_name.c_str(), B115200);
            g_serial = &serial;
            /* 订阅手动唤醒触发话题 */
            ros::NodeHandle nh_global;
            g_wakeup_sub = nh_global.subscribe("/wheeltec_mic/wakeup_trigger", 10, wakeup_trigger_callback);
            ROS_INFO(">>>>>已订阅 /wheeltec_mic/wakeup_trigger");

            // 主循环控制参数
            constexpr size_t READ_BUFFER_SIZE = 512;
            std::array<uint8_t, READ_BUFFER_SIZE> read_buffer;
            auto last_stat_print = ros::Time::now();
            auto last_connection_check = ros::Time::now();

            while(ros::ok()) {
                auto now = ros::Time::now();
                if ((now - last_connection_check).toSec() >= 5.0) { // 每5秒检查一次连接
                    if (!serial.check_connection()) {
                        ROS_WARN("串口连接丢失，尝试重新连接...");
                        throw std::runtime_error("Connection lost");
                    }
                    last_connection_check = now;
                }

                ssize_t bytes_read = serial.read_nonblock(read_buffer.data(), read_buffer.size());
                if(bytes_read > 0) {
                    for(ssize_t i=0; i<bytes_read; ++i) {
                        const uint8_t byte = read_buffer[i];

                        switch (parser.state) {
                            case ParseState::WAIT_HEADER:
                                if (byte == FRAME_HEADER) {
                                    parser.reset();
                                    parser.buffer.push_back(byte);
                                    parser.state = ParseState::CHECK_USER_ID;
                                }
                                break;

                            case ParseState::CHECK_USER_ID:
                                if (byte == USER_ID) {
                                    parser.buffer.push_back(byte);
                                    parser.state = ParseState::PARSE_TYPE;
                                } else {
                                    parser.reset();
                                }
                                break;

                            case ParseState::PARSE_TYPE:
                                parser.buffer.push_back(byte);
                                parser.message_type = byte;
                                parser.state = ParseState::PARSE_LENGTH;
                                break;

                            case ParseState::PARSE_LENGTH:
                                parser.buffer.push_back(byte);
                                if (parser.buffer.size() == 5) {
                                    parser.expected_length = parse_little_endian(&parser.buffer[3]);
                                    // 添加长度合理性检查，防止缓冲区溢出
                                    if (parser.expected_length > MAX_FRAME_SIZE - 8) {
                                        parser.reset();
                                    } else {
                                        parser.state = ParseState::PARSE_ID;
                                    }
                                }
                                break;

                            case ParseState::PARSE_ID:
                                parser.buffer.push_back(byte);
                                if (parser.buffer.size() == 7) {
                                    parser.message_id = parse_little_endian(&parser.buffer[5]);
                                    parser.state = ParseState::COLLECT_DATA;
                                }
                                break;

                            case ParseState::COLLECT_DATA:
                                parser.buffer.push_back(byte);
                                if (parser.buffer.size() == 7 + parser.expected_length) {
                                    parser.state = ParseState::VERIFY_CHECKSUM;
                                }
                                break;

                            case ParseState::VERIFY_CHECKSUM:
                                parser.buffer.push_back(byte);
                                if (calculate_checksum(parser.buffer) == byte) {
                                    process_valid_frame(parser.buffer, parser.message_type, parser.message_id, mic_processor);
                                } else {
                                    //ROS_WARN("Checksum mismatch");
                                }
                                parser.reset();
                                break;
                        }
                    }
                }

                ros::spinOnce();
                loop_rate.sleep();
            }
        }
        catch(const std::system_error& e) {
            ROS_ERROR("系统错误: %s (代码 %d)，将在5秒后重试...", e.what(), e.code().value());
            g_serial = nullptr;  // 防止回调访问已销毁的串口对象
            sleep(5);
        }
        catch(const std::exception& e) {
            ROS_ERROR("未处理的异常: %s，将在5秒后重试...", e.what());
            g_serial = nullptr;  // 防止回调访问已销毁的串口对象
            sleep(5);
        }
    }
    
    return 0;
}