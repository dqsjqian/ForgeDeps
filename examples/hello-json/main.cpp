#include <nlohmann/json.hpp>
#include <iostream>

int main() {
    const auto value = nlohmann::json::parse(R"({"message":"hello from ForgeDeps","answer":42})");
    if (value.at("answer").get<int>() != 42) return 1;
    std::cout << value.at("message").get<std::string>() << '\n';
}
