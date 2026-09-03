cmake_minimum_required(VERSION 3.8)
project(wheeltec_mic_aiui)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 14)
endif()

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

include_directories( /usr/include/alsa
                     /usr/local/include
                     ${CMAKE_CURRENT_LIST_DIR}/include
                     ${CMAKE_CURRENT_LIST_DIR}/src/utils
                     ${CMAKE_CURRENT_LIST_DIR}/src/jsoncpp
                     )

install(DIRECTORY launch AIUI
  DESTINATION share/${PROJECT_NAME})

# find dependencies
find_package(PkgConfig REQUIRED)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(std_msgs REQUIRED)
find_package(serial REQUIRED)
find_package(ollama_ros_msgs REQUIRED)
find_package(wheeltec_mic_msg REQUIRED)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

file(GLOB_RECURSE SOURCE_FILE
        "src/*.cpp")

link_directories(${CMAKE_CURRENT_LIST_DIR}/libs/x64)

add_executable(${PROJECT_NAME}  ${SOURCE_FILE})
ament_target_dependencies(${PROJECT_NAME} 
  rclcpp 
  std_msgs 
  serial 
  wheeltec_mic_msg
)
target_link_libraries(${PROJECT_NAME}
  aiui
  asound 
  samplerate 
  pthread 
  stdc++ 
  jsoncpp
)
target_include_directories(${PROJECT_NAME} PRIVATE 
  ${CMAKE_CURRENT_LIST_DIR}/include
  ${CMAKE_CURRENT_LIST_DIR}/src/utils
)

add_executable(command_recognition node/command_recognition.cpp)
ament_target_dependencies(command_recognition rclcpp rclcpp_action std_msgs wheeltec_mic_msg)

add_executable(wheeltec_mic node/wheeltec_mic.cpp node/ros2_service.cpp)
ament_target_dependencies(wheeltec_mic rclcpp std_msgs serial wheeltec_mic_msg)
target_link_libraries(wheeltec_mic jsoncpp)

add_executable(wheeltecM07 node/wheeltec_m07.cpp)
ament_target_dependencies(wheeltecM07 rclcpp std_msgs)

add_executable(wheeltecM2N node/wheeltec_m2n.cpp node/ros2_service_m2n.cpp)
ament_target_dependencies(wheeltecM2N rclcpp std_msgs serial wheeltec_mic_msg)
target_link_libraries(wheeltecM2N jsoncpp)

add_executable(chat_service  node/chat_service.cpp)
ament_target_dependencies(chat_service rclcpp std_msgs ollama_ros_msgs)

install(TARGETS
  ${PROJECT_NAME}
  command_recognition
  chat_service
  wheeltec_mic
  wheeltecM07
  wheeltecM2N
  RUNTIME DESTINATION lib/${PROJECT_NAME}    
)

ament_package()
