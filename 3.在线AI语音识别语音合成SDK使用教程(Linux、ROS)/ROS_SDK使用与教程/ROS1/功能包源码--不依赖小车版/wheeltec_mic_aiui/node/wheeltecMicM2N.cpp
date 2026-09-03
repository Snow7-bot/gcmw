/****************************************************************/
/* Copyright (c) 2026 WHEELTEC Technology, Inc                  */
/* function:Serial port analysis                                */
/* 功能：NEW_M2串口解析                                            */
/****************************************************************/
#include "wheeltec_m2n.h"

/**************************************
Function: Constructor, executed only once, for initialization
功能: 构造函数, 用于初始化
***************************************/
Wheeltec_Mic::Wheeltec_Mic(ros::NodeHandle nh, ros::NodeHandle private_nh)
    : nh_(nh), serial_initialized(false)
{
    memset(&Receive_Data, 0, sizeof(Receive_Data));

    // 获取参数（真实串口）
    private_nh.param<std::string>("usart_port_name", usart_port_name, "/dev/ttyCH343USB0");
    private_nh.param<int>("serial_baud_rate", serial_baud_rate, 115200);

    /***唤醒标志位话题发布者创建***/
    awake_flag_pub = nh_.advertise<std_msgs::Int8>("/awake_flag", 10);
    /***麦克风设备串口打开标志位话题发布者创建***/
    voice_flag_pub = nh_.advertise<std_msgs::Int8>("/voice_flag", 10);
    /***唤醒角度话题发布者创建***/
    angle_pub = nh_.advertise<std_msgs::UInt32>("/mic/awake/angle", 10);

    // 初始化两个串口
    initialize_serial();
}

Wheeltec_Mic::~Wheeltec_Mic()
{
    ROS_INFO("wheeltec_mic_node over!\n");

    if (timer_) {
        timer_.stop();
    }

    if (MicArr_Serial.isOpen()) {
        MicArr_Serial.close();
    }
    
}

/**************************************
Function: Get the serial port return field
功能: 获取串口返回字段
***************************************/
bool Wheeltec_Mic::UnPackMsgPacket(const std::string &content, MsgPacket &data)
{
    if (content.size() < 7 || ((unsigned char)content.at(0) != FRAME_HEADER))
        return false;

    data.uid  = content[1] & 0xff;
    data.type = content[2] & 0xff;
    
    data.size = (content[3] & 0xff) | ((content[4] & 0xff) << 8); 
    data.sid  = (content[5] & 0xff) | ((content[6] & 0xff) << 8);  

    // 验证长度
    if (content.size() < 7 + data.size + 1) {
        std::cout << "消息长度不足，需要" <<  7 + data.size + 1 << ", 实际:" << content.size() << std::endl;
        return false;
    }

    std::string info = content.substr(7, data.size);
    data.bytes = info;
    
    // 验证校验码
    unsigned char received_checksum = content[7 + data.size];
    unsigned char calculated_checksum = 0;
    for (int i = 0; i < 7 + data.size; i++) {
        calculated_checksum += content[i];
    }
    calculated_checksum = ((~calculated_checksum) + 1) & 0xFF;
    
    if (received_checksum != calculated_checksum) {
        std::cout << "校验码错误: 期望0x" <<  calculated_checksum << "实际0x" << received_checksum << std::endl;
        return false;
    }
    
    return true;
}

/********************************************************
Function: Send data packet
功能: 下发数据包
*********************************************************/
bool Wheeltec_Mic::Send_Serial_Data(ServicePacket &pkg)
{
    std::string section;
    std::string Master_Message;
    Json::Value type_describe;

    switch(pkg.msgType)
    {
        case ServiceType::DEVICE_VER:
            type_describe["type"]= pkg.type;
            break;
        case ServiceType::AWAKE_WORD:
        {
            type_describe["type"]= pkg.type;
            type_describe["content"]["keyword"] = pkg.awake_word;
            type_describe["content"]["threshold"] = pkg.threshold;
        }
            break;
        case ServiceType::SWITCH_MIC:
        {
            type_describe["type"]= pkg.type;
            type_describe["content"]["mic"] = pkg.mic_type;
        }
            break;
        case ServiceType::SET_BEAM:
            type_describe["type"]= pkg.type;
            type_describe["content"]["beam"] = pkg.beam;
            break;
        case ServiceType::SET_AUDIO:  
        {
            type_describe["type"]= pkg.type;
            type_describe["content"]["audio"] = pkg.mode;
        }
            break;
        default:
            break;
    }
    section = type_describe.toStyledString();
    //std::cout<< " section = "<< section <<std::endl;
    Master_Message = MakeMsgPacket(pkg.sid,MsgType::CONTROL,section);
    try
    {
        MicArr_Serial.write(Master_Message);
    }
    catch (serial::IOException& e)   
    {
        ROS_ERROR("Unable to send data through serial port"); 
        return false;
    }
    return true;
}

/********************************************************
Function: Make a packet
功能: 制作数据包
*********************************************************/
std::string Wheeltec_Mic::MakeMsgPacket(unsigned short sid, MsgType type, const std::string &content)
{
    const unsigned short size = content.size();

    std::string data;

    data += (char)FRAME_HEADER;             /* head     */
    data += (char)USER_ID;                  /* uid      */
    data += (char)type;                     /* type     */
    data += (char)(size & 0xff);            /* len_low  */
    data += (char)((size >> 8) & 0xff);     /* len_high */
    data += (char)(sid & 0xff);             /* sid_low  */
    data += (char)((sid >> 8) & 0xff);      /* sid_high */

    data += content;

    int sum = std::accumulate(data.cbegin(),data.cend(),0);

    data += (char)((~sum +1) & 0xff);

    return data;
}

/**************************************
Function: Verify serial port data and parse information
功能: 校验串口数据并解析信息
***************************************/
int Wheeltec_Mic::process_data(const unsigned char *buf, int len)
{
    if (len < 8) {
        std::cout << "数据长度不足: " << len << std::endl;
        return -1;
    }
    unsigned char msg_type = buf[2];
    // 计算消息长度（小端序）
    unsigned short reported_len = (buf[3] & 0xff) | ((buf[4] & 0xff) << 8);
    unsigned short total_len_needed = 7 + reported_len + 1;
    
    if (len != total_len_needed) {
        std::cout << ">>>>>长度不匹配"<< std::endl;
        return -1;
    }

    // 验证校验码（所有消息类型都需要验证）
    int sum = std::accumulate(buf, buf + len - 1, 0);
    unsigned char calculated_checksum = ((~sum) + 1) & 0xff;
    
    if (calculated_checksum != buf[len - 1]) {
        std::cout << ">>>>>校验码错误!"<< std::endl;
        return -1;
    }

    // ====================== 握手相关处理 ======================
    if (msg_type == 0x01) {
        if (!handshake_completed_) {
            std::cout << ">>>>>收到握手消息: "<< std::endl;
        }
        return sendHandshakeAck(buf, len);
    }
    // ====================== 握手处理结束 ======================

    // 使用统一的UnPackMsgPacket处理
    if (!UnPackMsgPacket(std::string((char *)buf, len), MsgPkg)) {
        std::cout << ">>>>>消息解析失败，类型: 0x" << msg_type << std::endl;
        return -1;
    }

    // ====================== 其他消息处理 ======================
    Json::Reader reader;
    Json::Value json_msg;
    Json::Value value_iwv;
    
    if ((MsgType)msg_type == MsgType::AIUI_MSG) {  // AIUI消息
        if (reader.parse(MsgPkg.bytes, json_msg)) {
            // 处理唤醒消息...
            if (json_msg["type"].asString() == "aiui_event") {
                std_msgs::Int8 awake_msg;
                awake_msg.data = 1;
                awake_flag_pub.publish(awake_msg);
                Json::Value content = json_msg["content"];
                if (content["eventType"].asString() == "4")
                {
                    std::string iwv_msg = content["info"].asString();
                    if (reader.parse(iwv_msg,value_iwv))
                    {
                        angle = value_iwv["ivw"]["angle"].asInt();
                        std_msgs::UInt32 angle_msg;
                        angle_msg.data = angle;
                        angle_pub.publish(angle_msg);
                        std::cout << ">>>>>唤醒角度为: " << angle << "°"<< std::endl;
                    }
                    else
                        std::cout << "reader json fail!"<< std::endl;
                }
            } else {
                device_message = json_msg["content"].asString();
                process_result = true;
            }
            return 1;
        }
    } 
    else if ((MsgType)msg_type == MsgType::CONTROL) {  // 控制消息
        if (reader.parse(MsgPkg.bytes, json_msg)) {
            // 打印完整的JSON结构
            std::cout << ">>>>>JSON内容: " <<  json_msg.toStyledString().c_str() << std::endl;

            if (json_msg.isMember("code") && json_msg.isMember("content")) {
                int code = json_msg["code"].asInt();
                std::string content = json_msg["content"].asString();
                
                // 设置设备消息和结果标志
                device_message = content;
                process_result = true;
                last_ack_id_ = MsgPkg.sid;  // 保存确认ID
                
                // 如果是音频请求确认
                if (content == "success" && json_msg.isMember("type")) {
                    std::string type = json_msg["type"].asString();
                    if (type == "get_original_audio") {
                        std::cout << ">>>>>收到音频请求确认: (ID: " <<  MsgPkg.sid << ")"<< std::endl;
                    }
                }
                return 1;
            }
        } else {
            std::cout << ">>>>>控制消息JSON解析失败"<< std::endl;
        }
    }
    
    return -1;
}

/********************************************************
Function: Start handshake process
功能:  发送握手确认
*********************************************************/
int Wheeltec_Mic::sendHandshakeAck(const unsigned char *buf, int len)
{
    if (len < 7) {
        return -1;
    }
    
    try {
        // 解析收到的握手消息ID
        unsigned short msg_id = (buf[5] & 0xff) | ((buf[6] << 8) & 0xff00);
        // 只在第一次发送握手确认时打印日志
        if (!handshake_completed_) {
            std::cout << ">>>>>已发送握手确认: (ID: " <<  msg_id << ")"<< std::endl;
        }
        // 构建握手确认数据 (固定为 0xA5 0x00 0x00 0x00)
        unsigned char handshake_ack_data[4] = {0xA5, 0x00, 0x00, 0x00};
        // 构建完整消息
        std::string ack_message = MakeMsgPacket(msg_id, MsgType::CONFIRM, 
                                          std::string((char*)handshake_ack_data, 4));
        // 发送握手确认
        MicArr_Serial.write(ack_message);
        handshake_completed_ = true;
        return 1;
    } 
    catch (const std::exception& e) {
        ROS_ERROR("Failed to send handshake confirmation: %s", e.what());
        return -1;
    }
}

/**************************************
Function: Receive and filter data (智能长度检测) - 第一个串口
功能: 过滤数据
***************************************/
int Wheeltec_Mic::uart_analyse_smart(unsigned char buffer)
{
    if (!serial_initialized) return false;
    
    static std::vector<unsigned char> rx_buffer;
    static int frame_count = 0;
    
    rx_buffer.push_back(buffer);
    
    while (rx_buffer.size() >= 7) {
        // 检查帧头
        if (rx_buffer[0] != FRAME_HEADER || rx_buffer[1] != USER_ID) {
            rx_buffer.erase(rx_buffer.begin());
            continue;
        }
        
        // 解析长度（小端序）
        unsigned short reported_len = (rx_buffer[3] & 0xff) | ((rx_buffer[4] & 0xff) << 8);
        unsigned short total_len = 7 + reported_len + 1;
        
        // 验证长度合理性
        if (total_len > 65536 || total_len < 8) {
            static int error_count = 0;
            if (error_count++ % 100 == 0) {
                std::cout << ">>>>>无效的消息长度: " << total_len << std::endl;
            }
            rx_buffer.erase(rx_buffer.begin());
            continue;
        }
        
        // 检查是否有完整帧
        if (rx_buffer.size() >= total_len) {
            frame_count++;
            
            // 处理帧
            int ret = process_data(rx_buffer.data(), total_len);
            
            // 删除已处理的数据
            if (total_len <= rx_buffer.size()) {
                rx_buffer.erase(rx_buffer.begin(), rx_buffer.begin() + total_len);
            } else {
                rx_buffer.clear();
            }
            
            return ret;
        } else {
            // 帧不完整，等待更多数据
            break;
        }
    }
    
    // 清理过大的缓冲区
    if (rx_buffer.size() > 65536) {
        std::cout << ">>>>>缓冲区过大，清空: " <<  rx_buffer.size() << "字节"<< std::endl;
        rx_buffer.clear();
    }
    
    return 0;
}

/**************************************
Function: Receive the information sent by the device
功能: 接收下位机发送的信息
***************************************/
bool Wheeltec_Mic::Get_Serial_Data()
{
    if (!serial_initialized) return false;

    try {
        size_t available = MicArr_Serial.available();
        if (available == 0) return false;
        
        // 一次性读取所有可用数据
        std::vector<unsigned char> buffer(available);
        size_t bytes_read = MicArr_Serial.read(buffer.data(), available);
        bool result = false;

        if (bytes_read > 0) {
            static int total_bytes_read = 0;
            total_bytes_read += bytes_read;
            
            // 批量处理所有数据
            for (size_t i = 0; i < bytes_read; i++) {
                // 使用uart_analyse_smart，因为它处理单个字节
                int ret = uart_analyse_smart(buffer[i]);
                if (ret != 0) {
                    result = true;
                }
            }
        }
        
        return result;
    } catch (const serial::IOException& e) {
        ROS_ERROR("Serial port reading error: %s", e.what());
        handle_serial_error();
    }
    return false;
}

/**************************************
Function: Handle serial port errors
功能: 处理串口异常
***************************************/
void Wheeltec_Mic::handle_serial_error() 
{
    serial_initialized = false;
    MicArr_Serial.close();
    
    ROS_INFO("Attempting to reconnect to main serial port...");
    for (int i = 0; i < 3; ++i) {
        try {
            MicArr_Serial.open();
            if (MicArr_Serial.isOpen()) {
                MicArr_Serial.flush();
                serial_initialized = true;
                ROS_INFO("Main serial port reconnected successfully");
                return;
            }
        } catch (...) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
    }
    ROS_ERROR("Failed to reconnect to main serial port");
}

/**************************************
Function: Serial port data processing callback
功能: 串口数据处理回调函数
***************************************/
void Wheeltec_Mic::serial_read_callback(const ros::TimerEvent& event)
{
    if (!serial_initialized) {
        ROS_ERROR("Serial port not initialized");
        return;
    } 
    Get_Serial_Data();
}

/**************************************
Function: Loop access to the lower computer data and issue topics
功能: 循环获取下位机数据与发布话题
***************************************/
void Wheeltec_Mic::run()
{
    if (!serial_initialized) {
        ROS_ERROR("Main serial port initialization failed");
    }

    // 创建定时器，20ms读取一次主串口数据
    timer_ = nh_.createTimer(ros::Duration(0.02), &Wheeltec_Mic::serial_read_callback, this);
    
    
    ROS_INFO("Wheeltec Mic Node started");
    ros::spin();
}

/**************************************
Function: Initialize serial port with retry
功能: 串口初始化
***************************************/
void Wheeltec_Mic::initialize_serial() {
    const int max_retries = 3;
    serial::Timeout timeout = serial::Timeout::simpleTimeout(1000); 
    for (int retry = 0; retry < max_retries; ++retry) {
        try {
            if (MicArr_Serial.isOpen()) MicArr_Serial.close();
            
            MicArr_Serial.setPort(usart_port_name);
            MicArr_Serial.setBaudrate(serial_baud_rate);
            MicArr_Serial.setTimeout(timeout);
            MicArr_Serial.open();
            
            if (MicArr_Serial.isOpen()) {
                MicArr_Serial.flush();
                serial_initialized = true;
                // ========== 清除串口缓存 ==========
                MicArr_Serial.flushInput();
                MicArr_Serial.flushOutput();
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                size_t bytes_to_discard = MicArr_Serial.available();
                if (bytes_to_discard > 0) {
                    std::vector<unsigned char> discard_buffer(bytes_to_discard);
                    MicArr_Serial.read(discard_buffer.data(), bytes_to_discard);
                }
                
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                
                handshake_completed_ = false;
                
                ROS_INFO("Real serial port initialized successfully: %s", usart_port_name.c_str());
                // ========== 缓存清理完成 ==========
                
                sleep(1.0);
                std_msgs::Int8 flag_msg;
                flag_msg.data = 1;
                voice_flag_pub.publish(flag_msg);
                std::cout << ">>>>>成功打开麦克风设备" << std::endl;
                std::cout << ">>>>>以降噪板设置的唤醒词为准[默认:小微小微] " << std::endl;
                return;
            }
        } catch (const std::exception& e) {
            ROS_ERROR("Real serial init attempt %d failed: %s", retry+1, e.what());
        }
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    ROS_FATAL("Failed to initialize real serial port after %d attempts", max_retries);
    ROS_ERROR("wheeltec_mic can not open real serial port,Please check the serial port cable! ");
}

int main(int argc, char **argv)
{
    ros::init(argc, argv, "wheeltec_mic");
    
    ros::NodeHandle nh;
    ros::NodeHandle private_nh("~");
    
    Wheeltec_Mic mic(nh, private_nh);
    mic.run();
    
    return 0;
}