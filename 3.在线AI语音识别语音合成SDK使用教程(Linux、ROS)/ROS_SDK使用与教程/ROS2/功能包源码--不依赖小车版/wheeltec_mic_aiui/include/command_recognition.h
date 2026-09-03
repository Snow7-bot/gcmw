#ifndef __CALL_COMMAND_RECOGNITION_H_
#define __CALL_COMMAND_RECOGNITION_H_

#include <iostream>
#include <vector>
#include <unistd.h>
#include <inttypes.h>
#include <feedback.h>
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/string.hpp>
#include <std_msgs/msg/int8.hpp>
#include <std_msgs/msg/int32.hpp>
using std::placeholders::_1;
using std::placeholders::_2;

class Command : public rclcpp::Node{
public:
	Command(const std::string &node_name,
         const rclcpp::NodeOptions &options);
	~Command();
	void run();

private:
	int voice_flag = 0;
	int awake_flag = 0;

	rclcpp::Publisher<std_msgs::msg::Int8>::SharedPtr awake_flag_pub;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr feedback_words_pub;

    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr voice_words_sub;

    void voice_flag_Callback(const std_msgs::msg::Int8::SharedPtr msg);
    void voice_words_Callback(const std_msgs::msg::String::SharedPtr msg);
	
};

#endif
