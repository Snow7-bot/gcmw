cmake_minimum_required(VERSION 3.0.2)
project(wheeltec_mic_aiui)

## Compile as C++11, supported in ROS Kinetic and newer
# add_compile_options(-std=c++11)
set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

link_directories(
  ${CMAKE_CURRENT_SOURCE_DIR}/libs/arm64
)

## Find catkin macros and libraries
## if COMPONENTS list like find_package(catkin REQUIRED COMPONENTS xyz)
## is used, also find other catkin packages
find_package(catkin REQUIRED COMPONENTS
  roscpp
  rospy
  std_msgs
  tf
  ollama_chat_ros
  message_generation
  serial
)

generate_messages(
  DEPENDENCIES
  std_msgs
)

catkin_package(
  #INCLUDE_DIRS include
  #LIBRARIES turn_on_wheeltec_robot
  #CATKIN_DEPENDS geometry_msgs message_generation nav_msgs nav_msgs roscpp roslib rospy sensor_msgs std_msgs tf
  #DEPENDS system_lib
  CATKIN_DEPENDS message_runtime
)

## Specify additional locations of header files
## Your package locations should be listed before other locations
include_directories(
  ${catkin_INCLUDE_DIRS}
  /usr/include/alsa
  ${CMAKE_CURRENT_SOURCE_DIR}/include
  ${CMAKE_CURRENT_SOURCE_DIR}/src/utils
  ${CMAKE_CURRENT_SOURCE_DIR}/src/jsoncpp
)

file(GLOB_RECURSE SOURCE_FILES
  "src/*.cpp"
)

## Declare a C++ executable
## With catkin_make all packages are built within a single CMake context
## The recommended prefix ensures that target names across packages don't collide
# add_executable(${PROJECT_NAME}_node src/wheeltec_aiui_ros_node.cpp)
add_executable(${PROJECT_NAME} ${SOURCE_FILES})

target_link_libraries(${PROJECT_NAME}
  ${catkin_LIBRARIES} aiui asound samplerate pthread stdc++)

add_executable(commandRecognition node/commandRecognition.cpp)
add_dependencies(commandRecognition ${PROJECT_NAME}_generate_messages_cpp)
target_link_libraries(commandRecognition ${catkin_LIBRARIES})

add_executable(chatService node/chatService.cpp)
target_link_libraries(chatService ${catkin_LIBRARIES})

add_executable(wheeltecMic node/wheeltecMic.cpp)
target_link_libraries(wheeltecMic
  ${catkin_LIBRARIES} -ljsoncpp)

add_executable(wheeltecMicM2N node/wheeltecMicM2N.cpp)
target_link_libraries(wheeltecMicM2N
  ${catkin_LIBRARIES} -ljsoncpp)
  
add_executable(wheeltecM07 node/wheeltecM07.cpp)
target_link_libraries(wheeltecM07 ${catkin_LIBRARIES})

