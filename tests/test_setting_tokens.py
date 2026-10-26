import unittest

from forgedeps.environments import _setting_tokens


class SettingComparisonTests(unittest.TestCase):
    def test_flag_whitespace_is_not_an_abi_difference(self):
        self.assertEqual(_setting_tokens("CMAKE_CXX_FLAGS_DEBUG", "/Zi /Ob0 /Od "),
                         _setting_tokens("CMAKE_CXX_FLAGS_DEBUG", "/Zi /Ob0 /Od"))
        self.assertEqual(_setting_tokens("CMAKE_C_FLAGS", " -O2  -g"),
                         _setting_tokens("CMAKE_C_FLAGS", "-O2 -g"))

    def test_flag_order_and_content_still_matter(self):
        self.assertNotEqual(_setting_tokens("CMAKE_CXX_FLAGS", "/O2 /GL"),
                            _setting_tokens("CMAKE_CXX_FLAGS", "/GL /O2"))
        self.assertNotEqual(_setting_tokens("CMAKE_CXX_FLAGS_DEBUG", "/Zi /Od"),
                            _setting_tokens("CMAKE_CXX_FLAGS_DEBUG", "/Zi /Od /RTC1"))

    def test_non_flag_settings_compare_exactly(self):
        self.assertNotEqual(_setting_tokens("CMAKE_MSVC_RUNTIME_LIBRARY", "MultiThreadedDebugDLL "),
                            _setting_tokens("CMAKE_MSVC_RUNTIME_LIBRARY", "MultiThreadedDebugDLL"))


if __name__ == "__main__":
    unittest.main()
