/************************************************************************************************/
/* Copyright (c) 2025 WHEELTEC Technology, Inc   												*/
/* function:Command controller, command word recognition results into the corresponding action	*/
/* 功能：命令控制器，命令词识别结果转化为对应的执行动作													*/
/************************************************************************************************/
#include <ros/ros.h>
#include <geometry_msgs/Twist.h>
#include <std_msgs/String.h>
#include <iostream>
#include <stdio.h>
#include <feedback.h>
#include <std_msgs/Int8.h>
#include <geometry_msgs/PoseStamped.h>
#include <stdlib.h>

int voice_flag = 0; 
ros::Publisher feedback_words_pub;		//语音反馈发布者
ros::Publisher awake_flag_pub;    		//创建唤醒标志位话题发布者

/**************************************************************************
函数功能：离线命令词识别结果sub回调函数
入口参数：命令词字符串  aiui_node等
返回  值：无
**************************************************************************/
void voice_words_callback(const std_msgs::String& msg)
{
	/***指令***/
	std::string str1 = msg.data.c_str();    //取传入数据
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
	std::string str12 = "遇到障碍物";
	std::string str13 = "小车唤醒";
	std::string str14 = "小车雷达跟随";
	std::string str15 = "小车色块跟随";
	std::string str16 = "关闭雷达跟随";
	std::string str17 = "关闭色块跟随";
	std::string str18 = "打开自主建图";
	std::string str19 = "关闭自主建图";
	std::string str20 = "开始导航";

/***********************************
指令：小车前进
动作：底盘运动控制器使能，发布速度指令
***********************************/
	if(str1 == str2)
	{
		feedback_text.data = "小车前进";
		feedback_words_pub.publish(feedback_text);	
		std::cout<<"好的：小车前进"<<std::endl;
	}
/***********************************
指令：小车后退
动作：底盘运动控制器使能，发布速度指令
***********************************/
	else if(str1 == str3)
	{
		feedback_text.data = "小车后退";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车后退"<<std::endl;
	}
/***********************************
指令：小车左转
动作：底盘运动控制器使能，发布速度指令
***********************************/
	else if(str1 == str4)
	{
		feedback_text.data = "小车左转";
		feedback_words_pub.publish(feedback_text);      
		std::cout<<"好的：小车左转"<<std::endl;
	}
/***********************************
指令：小车右转
动作：底盘运动控制器使能，发布速度指令
***********************************/
	else if(str1 == str5)
	{
		feedback_text.data = "小车右转";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车右转"<<std::endl;
	}
/***********************************
指令：小车停
动作：底盘运动控制器失能，发布速度空指令
***********************************/
	else if(str1 == str6)
	{
		feedback_text.data = "小车停";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车停"<<std::endl;
	}
/***********************************
指令：小车休眠
动作：底盘运动控制器失能，发布速度空指令，唤醒标志位置零
***********************************/
	else if(str1 == str7)
	{
		feedback_text.data = "小车休眠";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"小车休眠，等待下一次唤醒"<<std::endl;
	}
/***********************************
指令：小车过来
动作：寻找声源标志位置位
***********************************/
	else if(str1 == str8)
	{
		feedback_text.data = "小车寻找声源";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车寻找声源"<<std::endl;
	}
/***********************************
指令：小车去I点
动作：底盘运动控制器失能(导航控制)，发布目标点
***********************************/
	else if(str1 == str9)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车自主导航至I点"<<std::endl;
		
	}
/***********************************
指令：小车去I点
动作：底盘运动控制器失能(导航控制)，发布目标点
***********************************/
	else if(str1 == str10)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车自主导航至J点"<<std::endl;
	}
/***********************************
指令：小车去K点
动作：底盘运动控制器失能(导航控制)，发布目标点
***********************************/
	else if(str1 == str11)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车自主导航至K点"<<std::endl;
	}
/***********************************
辅助指令：遇到障碍物
动作：用户界面打印提醒
***********************************/
	else if(str1 == str12)
	{
		feedback_text.data = "遇到障碍物";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"小车遇到障碍物，已停止运动"<<std::endl;
	}
/***********************************
辅助指令：小车唤醒
动作：用户界面打印提醒
***********************************/
	else if(str1 == str13)
	{
		std::cout<<"小车已被唤醒，请说语音指令"<<std::endl;
		
	}
	else if(str1 == str14)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车雷达跟随"<<std::endl;
	}
	else if(str1 == str15)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车色块跟随"<<std::endl;
	}
	else if(str1 == str16)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：关闭雷达跟随"<<std::endl;
	}
	else if(str1 == str17)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：关闭色块跟随"<<std::endl;

	}
	else if(str1 == str18)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：打开自主建图"<<std::endl;

	}
	else if(str1 == str19)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：关闭自主建图"<<std::endl;
	}
	else if(str1 == str20)
	{
		feedback_text.data = "好的";
		feedback_words_pub.publish(feedback_text);
		std::cout<<"好的：小车开始导航"<<std::endl;
	}
}

/**************************************************************************
函数功能：寻找语音开启成功标志位sub回调函数
入口参数：voice_flag_msg  voice_control.cpp
返回  值：无
**************************************************************************/
void voice_flag_Callback(std_msgs::Int8 msg)
{
	voice_flag = msg.data;
	if(voice_flag == 1){
		feedback_text.data = "语音打开成功";
		feedback_words_pub.publish(feedback_text);
	}

}

/**************************************************************************
函数功能：主函数
入口参数：无
返回  值：无
**************************************************************************/
int main(int argc, char** argv)
{

	ros::init(argc, argv, "cmd_rec"); 

	ros::NodeHandle n;    			
	
	/***创建唤醒标志位话题发布者***/
	awake_flag_pub = n.advertise<std_msgs::Int8>("awake_flag", 1);
	/***创建唤醒标志位话题发布者***/
	feedback_words_pub = n.advertise<std_msgs::String>("feedback_words", 1);

	/***创建离线命令词识别结果话题订阅者***/
	ros::Subscriber voice_words_sub = n.subscribe("voice_words",1,voice_words_callback);
	/***创建寻找语音开启标志位话题订阅者***/
	ros::Subscriber voice_flag_sub = n.subscribe("voice_flag", 1, voice_flag_Callback);

	std::cout<<"您可以语音控制啦！以降噪板设置的唤醒词为准[默认:“小微小微”]"<<std::endl;
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
	std::cout<<"小车色块跟随———————————>小车打开色块跟随"<<std::endl;
	std::cout<<"关闭色块跟随———————————>小车关闭色块跟随"<<std::endl;
	std::cout<<"打开自主建图———————————>小车打开自主建图"<<std::endl;
	std::cout<<"关闭自主建图———————————>关闭打开自主建图"<<std::endl;
	std::cout<<"开始导航———————————>小车开始导航"<<std::endl;
	std::cout<<"\n"<<std::endl;
	ros::spin();
}
