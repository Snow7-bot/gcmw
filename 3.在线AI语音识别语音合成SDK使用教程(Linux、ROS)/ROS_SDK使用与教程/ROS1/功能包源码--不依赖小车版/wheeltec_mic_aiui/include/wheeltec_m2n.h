#ifndef __WHEELTEC_MIC_H_
#define __WHEELTEC_MIC_H_

#include <vector>
#include <memory>
#include <functional>
#include <atomic>
#include <string>
#include <sys/stat.h>
#include <iostream>
#include <fstream>
#include <unistd.h>
#include <numeric>
#include <chrono>
#include <thread>
#include <serial/serial.h>
#include <std_msgs/Int8.h>
#include <std_msgs/String.h>
#include <std_msgs/UInt32.h>
#include "jsoncpp/json/json.h"
#include "ros/ros.h"

#define FRAME_HEADER        0XA5        
#define USER_ID             0X01        
#define TIMEOUT             10.0

enum class MsgType : unsigned char {
    Shake =             0x01,
    AIUI_MSG =          0x04,
    CONTROL =           0x05,
    VOICE =             0x06,
    CONFIRM =           0xFF
};

enum class ServiceType{
    AWAKE_WORD,
    SWITCH_MIC,
    DEVICE_VER,
    SET_AUDIO,
    SET_BEAM
};

struct ServicePacket
{
    unsigned short sid;

    int beam;
    int mode;
    std::string type;
    std::string threshold;
    std::string awake_word;
    std::string mic_type;
    std::string content;

    ServiceType msgType;
};

struct MsgPacket
{
    unsigned char uid;
    unsigned char type;

    unsigned short size;
    unsigned short sid;

    std::string bytes;
};

class Wheeltec_Mic {
public:
    Wheeltec_Mic(ros::NodeHandle nh, ros::NodeHandle private_nh);
    ~Wheeltec_Mic();
    void run();
    serial::Serial MicArr_Serial;

private:
    int angle,serial_baud_rate;
    bool process_result;
    bool serial_initialized;
    bool handshake_completed_ = false;
    unsigned char Receive_Data[1024] = {0};
    std::string device_message,usart_port_name;
    unsigned short last_ack_id_ = 0;        // 最后确认的ID

    MsgPacket MsgPkg;

    ros::Timer timer_;
    ros::Time start_time, last_time;
    ros::NodeHandle nh_;
    ros::Publisher angle_pub;
    ros::Publisher voice_words_pub;
    ros::Publisher awake_flag_pub,voice_flag_pub;

    bool Get_Serial_Data();
    bool UnPackMsgPacket(const std::string &content, MsgPacket &data);
    void handle_serial_error();
    void serial_read_callback(const ros::TimerEvent& event);
    void initialize_serial();
    std::string MakeMsgPacket(unsigned short sid, MsgType type, const std::string &content);

    bool Send_Serial_Data(ServicePacket &pkg);
    int process_data(const unsigned char *buf, int len);
    int sendHandshakeAck(const unsigned char *buf, int len);
    int uart_analyse_smart(unsigned char buffer);
};

#endif