#ifndef CHAT_H_
#define CHAT_H_

#include <iostream>
#include "ros/ros.h"
#include "std_msgs/String.h"
#include "std_msgs/Int8.h"
#include <ollama_chat_ros/Chat.h>

class  Chat_Node {
public:
    Chat_Node();
    ~Chat_Node();
    void sendMessage(const std::string& message);
     std::string removeTags(const std::string& input);

private:
    bool waiting_for_response_ = false;
    ros::Subscriber voice_words_sub;
    ros::Publisher chat_words_pub;
    ros::ServiceClient chat_client;

    void voice_words_callback(const std_msgs::String& msg);
};

#endif /* CHAT_H_ */