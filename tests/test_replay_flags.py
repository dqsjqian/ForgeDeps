"""A recipe's own defines must survive the toolchain flag replay."""
import unittest

from forgedeps.environments import merge_flag_value, recipe_flag_values


class RecipeFlagReplayTests(unittest.TestCase):
    def test_flag_options_are_read_with_and_without_a_type(self):
        values = recipe_flag_values([
            '-DCMAKE_C_FLAGS=/DWIN32 /D_WINDOWS -DNGTCP2_STATICLIB',
            '-DCMAKE_CXX_FLAGS:STRING=/EHsc',
            '-DCMAKE_C_FLAGS_DEBUG=',
            '-DUSE_NGTCP2=ON',
        ])
        self.assertEqual(values['CMAKE_C_FLAGS'],
                         '/DWIN32 /D_WINDOWS -DNGTCP2_STATICLIB')
        self.assertEqual(values['CMAKE_CXX_FLAGS'], '/EHsc')
        # An empty value carries no define, so the replay value stands alone.
        self.assertNotIn('CMAKE_C_FLAGS_DEBUG', values)
        self.assertNotIn('USE_NGTCP2', values)

    def test_replay_value_is_appended_behind_the_recipe_defines(self):
        merged = merge_flag_value('/DWIN32 /D_WINDOWS -DNGTCP2_STATICLIB',
                                  '/DWIN32 /D_WINDOWS')
        self.assertEqual(merged, '/DWIN32 /D_WINDOWS -DNGTCP2_STATICLIB')
        self.assertEqual(merge_flag_value('', '/O2'), '/O2')
        self.assertEqual(merge_flag_value('/DGumbo', ''), '/DGumbo')

    def test_already_contained_replay_value_is_not_duplicated(self):
        self.assertEqual(merge_flag_value('/O2 -DNGHTTP3_STATICLIB', '/O2'),
                         '/O2 -DNGHTTP3_STATICLIB')


if __name__ == '__main__':
    unittest.main()
