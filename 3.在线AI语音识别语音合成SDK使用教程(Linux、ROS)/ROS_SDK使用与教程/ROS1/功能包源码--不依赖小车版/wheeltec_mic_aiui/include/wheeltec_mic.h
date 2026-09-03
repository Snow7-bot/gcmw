#include <ros/ros.h>
#include <iostream>
#include <vector>
#include <memory>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <poll.h>
#include <sys/time.h>
#include <string.h>
#include <array>
#include <system_error>
#include <std_msgs/Int32.h>
#include <std_msgs/Int8.h>
#include <std_msgs/String.h>
#include <cjson/cJSON.h>
#include "jsoncpp/json/json.h"
#include <numeric>

// 参数配置
std::string port_name;
std::string awake_words_str = "以降噪板设置的唤醒词为准[默认:小微小微]";
// 协议常量定义
constexpr uint8_t FRAME_HEADER = 0xA5;
constexpr uint8_t USER_ID = 0x01;
constexpr size_t MAX_FRAME_SIZE = 1024;

ros::Publisher voice_flag_pub;
class EnhancedSerial;
EnhancedSerial* g_serial = nullptr;
ros::Subscriber g_wakeup_sub;

// 协议解析状态机
enum class ParseState {
    WAIT_HEADER,
    CHECK_USER_ID,
    PARSE_TYPE,
    PARSE_LENGTH,
    PARSE_ID,
    COLLECT_DATA,
    VERIFY_CHECKSUM
};

// 串口资源管理类
class SerialPort {
public:
	SerialPort(const char* port, int baud);
	~SerialPort();
	bool check_connection();
	ssize_t read_nonblock(unsigned char* buf, size_t size);
	int fd;
private:
	bool configure(int baud);
};

// 增强型串口类（带超时重试）
class EnhancedSerial : public SerialPort {
public:
    using SerialPort::SerialPort;
    bool reliable_write(const uint8_t* data, size_t len, int retries);
};

class MicProcessor {
public:
    MicProcessor(ros::NodeHandle& nh);
    void process_awake_event(int angle);

private:
    ros::NodeHandle& nh;
    ros::Publisher awake_flag_pub;
    ros::Publisher voice_words_pub;
    ros::Publisher pub_awake_angle;
};
// 手动唤醒：构建协议包并发送给 M2
std::string MakeMsgPacket(unsigned short sid, unsigned char type, const std::string &content);

// 手动唤醒：ROS 话题回调
void wakeup_trigger_callback(const std_msgs::String& msg);
#endif
