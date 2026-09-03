#include "chat_service.h"

/**************************************************************************
函数功能：识别结果sub回调函数
**************************************************************************/
void Chat_Node::voice_words_callback(const std_msgs::String& msg){
    std::string chat_text =  msg.data.c_str();    //取传入数据
    sendMessage(chat_text);
}

/**************************************************************************
函数功能：对话服务请求发送
**************************************************************************/
void Chat_Node::sendMessage(const std::string& message) {
    ollama_chat_ros::Chat srv;
    srv.request.content = message;
    waiting_for_response_ = true;
    std::cout << "正在思索整理中..." << std::endl;

    try {
        if (chat_client.call(srv)) {
            std::string rmText = removeTags(srv.response.content);
            std::cout << rmText << std::endl;
            std_msgs::String result_text;
            result_text.data = rmText;
            chat_words_pub.publish(result_text);
            waiting_for_response_ = false;
        }
    } catch (const ros::Exception& e) {
        ROS_ERROR("Service call failed: %s", e.what());
    } 
}

/**************************************************************************
函数功能：移除think标签
**************************************************************************/
std::string Chat_Node::removeTags(const std::string& input) {
    std::string result = input;
    size_t start_pos = 0;
    // 取消思考过程输出(移除 <think> 和 </think> 及其之间的内容)
    while ((start_pos = result.find("<think>", start_pos)) != std::string::npos) {
        size_t end_pos = result.find("</think>", start_pos);
        if (end_pos == std::string::npos) {
            // 如果没有找到对应的结束标签，直接返回结果
            break;
        }
        // 计算需要移除的部分长度
        size_t length_to_remove = end_pos - start_pos + strlen("</think>");
        result.erase(start_pos, length_to_remove);
        // 更新搜索起点
        start_pos = start_pos;
    }
    return result;
}

Chat_Node::Chat_Node() {
    ROS_INFO("Chat_Node init!");

    ros::NodeHandle nh_("~");
    /***服务客户端创建***/
    chat_client = nh_.serviceClient<ollama_chat_ros::Chat>("/chat_service");
    /***对话文本话题发布者创建***/
    chat_words_pub = nh_.advertise<std_msgs::String>("/feedback_words", 10);
    /***识别结果话题订阅者创建***/
    voice_words_sub = nh_.subscribe("/voice_words",1, &Chat_Node::voice_words_callback, this);
}

Chat_Node::~Chat_Node(){
    ROS_INFO("Chat_Node over!");
}

int main(int argc, char** argv)
{
    ros::init(argc, argv, "Chat_Node");
    auto node = std::make_shared<Chat_Node>();
    ros::spin();
    return 0;
}