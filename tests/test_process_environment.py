"""Child environments must never carry case-variant duplicate names.

MSBuild rejects an environment block that holds two spellings of one Windows
variable, and CPython before 3.11 forwards both without complaint. These tests
drive the Windows rules from a POSIX host through the explicit switch.
"""
import unittest

from forgedeps.process_environment import case_insensitive_keys, merge_environments, select_environment
from forgedeps.store import BUILD_ENVIRONMENT_KEYS


class CaseSensitiveMergeTests(unittest.TestCase):
    def test_posix_keeps_distinct_names(self):
        merged = merge_environments({"PATH": "one"}, {"Path": "two"}, case_insensitive=False)
        self.assertEqual(merged, {"PATH": "one", "Path": "two"})

    def test_later_layer_wins_within_one_spelling(self):
        merged = merge_environments({"CC": "clang"}, {"CC": "gcc"}, case_insensitive=False)
        self.assertEqual(merged, {"CC": "gcc"})

    def test_default_follows_the_host_platform(self):
        self.assertEqual(case_insensitive_keys(), __import__("os").name == "nt")


class WindowsCaseInsensitiveMergeTests(unittest.TestCase):
    def test_recipe_spelling_replaces_the_inherited_uppercase_copy(self):
        # os.environ on Windows exposes WINDOWSSDKLIBVERSION while recipes use
        # the documented WindowsSDKLibVersion spelling.
        merged = merge_environments({"WINDOWSSDKLIBVERSION": "10.0.26100.0"},
                                    {"WindowsSDKLibVersion": "10.0.26100.0"},
                                    case_insensitive=True)
        self.assertEqual(merged, {"WindowsSDKLibVersion": "10.0.26100.0"})

    def test_case_variants_never_survive_one_merge(self):
        merged = merge_environments({"Path": "one", "PATH": "two", "pAtH": "three"},
                                    case_insensitive=True)
        self.assertEqual(list(merged), ["pAtH"])
        self.assertEqual(merged["pAtH"], "three")
        self.assertEqual(len({name.casefold() for name in merged}), len(merged))

    def test_later_layer_value_wins_across_spellings(self):
        merged = merge_environments({"ANDROID_PLATFORM": "android-21"},
                                    {"android_platform": "android-24"},
                                    case_insensitive=True)
        self.assertEqual(merged, {"android_platform": "android-24"})

    def test_all_layers_are_optional(self):
        self.assertEqual(merge_environments(None, {}, None, case_insensitive=True), {})


class SelectEnvironmentTests(unittest.TestCase):
    def test_uppercase_ambient_satisfies_mixed_case_recipe_name(self):
        selected = select_environment(("WindowsSDKLibVersion", "VCToolsVersion"),
                                      {"WINDOWSSDKLIBVERSION": "10.0.26100.0",
                                       "VCTOOLSVERSION": "14.51.36231"},
                                      case_insensitive=True)
        self.assertEqual(selected, {"WindowsSDKLibVersion": "10.0.26100.0",
                                    "VCToolsVersion": "14.51.36231"})

    def test_posix_requires_the_exact_spelling(self):
        selected = select_environment(("WindowsSDKLibVersion",),
                                      {"WINDOWSSDKLIBVERSION": "10.0.26100.0"},
                                      case_insensitive=False)
        self.assertEqual(selected, {})

    def test_empty_values_are_dropped_and_output_is_sorted(self):
        selected = select_environment(("B", "A", "C"), {"A": "1", "B": "2", "C": ""},
                                      case_insensitive=False)
        self.assertEqual(list(selected), ["A", "B"])

    def test_every_declared_build_key_is_reachable_on_windows(self):
        ambient = {name.upper(): "value-of-" + name for name in BUILD_ENVIRONMENT_KEYS}
        selected = select_environment(BUILD_ENVIRONMENT_KEYS, ambient, case_insensitive=True)
        self.assertEqual(set(selected), set(BUILD_ENVIRONMENT_KEYS))


if __name__ == "__main__":
    unittest.main()
