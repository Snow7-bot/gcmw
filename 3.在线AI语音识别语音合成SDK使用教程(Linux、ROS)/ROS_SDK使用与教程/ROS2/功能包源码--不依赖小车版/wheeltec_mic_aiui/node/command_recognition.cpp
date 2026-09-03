/************************************************************************************************/
/* Copyright (c) 2025 WHEELTEC Technology, Inc   												*/
/* function:Command controller, command word recognition results into the corresponding action	*/
/* 功能：命令控制器，命令词识别结果转化为对应的执行动作													*/
/************************************************************************************************/
#include "command_recognition.h"
using std::placeholders::_1;

/**************************************************************************
函数功能：寻找语音开启成功标志位sub回调函数
入口参数：voice_flag_msg  voice_control.cpp
返回  值：无
**************************************************************************/
void Command::voice_flag_Callback(std_msgs::msg::Int8::SharedPtr msg){
	voice_flag = msg->data;
	if (voice_flag){
		feedback_text.data = "语音打开成功";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"语音打开成功"<<std::endl;
	}
}

/**************************************************************************
函数功能：识别结果sub回调函数
入口参数：命令词字符串
返回  值：无
**************************************************************************/
void Command::voice_words_Callback(std_msgs::msg::String::SharedPtr msg){
	/***语音指令***/
	std::string str1 = msg->data;    //取传入数据
	std::string str2 = "小车前进";
	std::string str3 = "小车后退"; 
	std::string str4 = "小车左转";
	std::string str5 = "小车右转";
	std::string str6 = "小车停";
	std::string str7 = "小车休眠";
	std::string str8 = "小车过来";
	std::string str9 = "小车去i点";
	std::string str10 = "小车去j点";
	std::string str11 = "小车去k点";
	std::string str12 = "失败5次";
	std::string str13 = "失败10次";
	std::string str14 = "遇到障碍物";
	std::string str15 = "小车雷达跟随";
	std::string str16 = "关闭雷达跟随";
/***********************************
指令：小车前进
动作：底盘运动控制器使能，发布速度指令
***********************************/
	if (str1 == str2){
		feedback_text.data = "小车前进";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车前进"<<std::endl;
	}
/***********************************
指令：小车后退
动作：底盘运动控制器使能，发布速度指令
***********************************/
	else if (str1 == str3){
		feedback_text.data = "小车后退";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车后退"<<std::endl;
	}
/***********************************
指令：小车左转
动作：底盘运动控制器使能，发布速度指令
***********************************/
	else if (str1 == str4){
		feedback_text.data = "小车左转";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车左转"<<std::endl;
	}
/***********************************
指令：小车右转
动作：底盘运动控制器使能，发布速度指令
***********************************/
	else if (str1 == str5){
		feedback_text.data = "小车右转";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车右转"<<std::endl;
	}
/***********************************
指令：小车停
动作：底盘运动控制器失能，发布速度空指令
***********************************/
	else if (str1 == str6){
		feedback_text.data = "小车停";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车停"<<std::endl;
	}
/***********************************************
指令：小车休眠
动作：底盘运动控制器失能，发布速度空指令，唤醒标志位置零
***********************************************/
	else if (str1 == str7){
		std_msgs::msg::Int8 awake_flag_msg;
		awake_flag_msg.data = 0;
		awake_flag_pub->publish(awake_flag_msg);

		feedback_text.data = "小车休眠";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"小车休眠，等待下一次唤醒"<<std::endl;
	}
/***********************************
指令：小车过来
动作：寻找声源标志位置位
***********************************/
	else if (str1 == str8){
		feedback_text.data = "小车寻找声源";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车寻找声源"<<std::endl;
	}
/***********************************
指令：小车去I点
动作：底盘运动控制器失能(导航控制)，发布目标点
***********************************/
	else if (str1 == str9){
		feedback_text.data = "好的";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车自主导航至I点"<<std::endl;
	}
/***********************************
指令：小车去J点
动作：底盘运动控制器失能(导航控制)，发布目标点
***********************************/
	else if (str1 == str10){
		feedback_text.data = "好的";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车自主导航至J点"<<std::endl;
	}
/***********************************
指令：小车去K点
动作：底盘运动控制器失能(导航控制)，发布目标点
***********************************/
	else if (str1 == str11){
		feedback_text.data = "好的";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车自主导航至K点"<<std::endl;
	}
	else if (str1 == str12){
		std::cout<<"您已经连续【输入空指令or识别失败】5次，累计达15次自动进入休眠，输入有效指令后计数清零"<<std::endl;
	}
	else if (str1 == str13){
		std::cout<<"您已经连续【输入空指令or识别失败】10次，累计达15次自动进入休眠，输入有效指令后计数清零"<<std::endl;
	}
/***********************************
辅助指令：遇到障碍物
动作：用户界面打印提醒
***********************************/
	else if (str1 == str14){
		feedback_text.data = "遇到障碍物";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"小车遇到障碍物，已停止运动"<<std::endl;
	}

/***********************************
辅助指令：小车雷达跟随
动作：用户界面打印提醒并开启节点
***********************************/
	else if (str1 == str15){
		feedback_text.data = "好的";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：小车雷达跟随"<<std::endl;
	}
/***********************************
辅助指令：关闭雷达跟随
动作：用户界面打印提醒并关闭节点
***********************************/
	else if (str1 == str16 ){
		feedback_text.data = "好的";
		feedback_words_pub->publish(feedback_text);
		std::cout<<"好的：关闭雷达跟随"<<std::endl;
	}
}

Command::Command(const std::string &node_name,
	const rclcpp::NodeOptions &options)
: rclcpp::Node(node_name,options){
	RCLCPP_INFO(this->get_logger(),"%s node init!\n",node_name.c_str());

	/***唤醒标志位话题发布者创建***/
	awake_flag_pub = this->create_publisher<std_msgs::msg::Int8>("awake_flag",10); 
	/***语音反馈文本发布者创建***/
	feedback_words_pub = this->create_publisher<std_msgs::msg::String>("feedback_words",10);
	/***识别结果话题订阅者创建***/
	voice_words_sub = this->create_subscription<std_msgs::msg::String>(
		"voice_words",10,std::bind(&Command::voice_words_Callback,this,_1));

	std::cout<<"您可以语音控制啦!"<<std::endl;
	std::cout<<"小车前进———————————>向前"<<std::endl;
	std::cout<<"小车后退———————————>后退"<<std::endl;
	std::cout<<"小车左转———————————>左转"<<std::endl;
	std::cout<<"小车右转———————————>右转"<<std::endl;
	std::cout<<"小车停———————————>停止"<<std::endl;
	std::cout<<"小车休眠———————————>休眠，等待下一次唤醒"<<std::endl;
	std::cout<<"小车过来———————————>寻找声源"<<std::endl;
	std::cout<<"小车去I点———————————>小车自主导航至I点"<<std::endl;
	std::cout<<"小车去J点———————————>小车自主导航至J点"<<std::endl;
	std::cout<<"小车去K点———————————>小车自主导航至K点"<<std::endl;
	std::cout<<"小车雷达跟随———————————>小车打开雷达跟随"<<std::endl;
	std::cout<<"关闭雷达跟随———————————>小车关闭雷达跟随"<<std::endl;
}

void Command::run(){
	rclcpp::spin(shared_from_this());
}

Command::~Command(){
	RCLCPP_INFO(this->get_logger(),"command_recognition_node over!\n");
}

int main(int argc, char *argv[])
{
	rclcpp::init(argc,argv);
	auto node = std::make_shared<Command>("command_recognition",rclcpp::NodeOptions());
	rclcpp::spin(node);  
	rclcpp::shutdown();
	return 0;
}