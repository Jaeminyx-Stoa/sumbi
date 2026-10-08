"""Private edit operands retain multi-file patch structure and conservative gaps."""

import json
import unittest

from sumbi.events.edit_targets import patch_targets


class PatchTargetTests(unittest.TestCase):
    def test_raw_and_structured_patch_inputs_retain_all_headers(self):
        text = ("*** Begin Patch\n*** Add File: added.py\n+pass\n"
            "*** Update File: old.py\n*** Move to: new.py\n@@\n-old\n+new\n"
            "*** Delete File: deleted.py\n*** End Patch")
        expected = ("added.py", "old.py", "new.py", "deleted.py")
        for value, cwd in ((text, None), ({"patch": text, "cwd": "/synthetic"}, "/synthetic"),
            (json.dumps({"input": text, "workdir": "/synthetic"}), "/synthetic")):
            self.assertEqual(patch_targets(value), (expected, cwd))

    def test_missing_malformed_and_payload_headers_are_not_parsed_as_targets(self):
        for text in (None, {}, "synthetic edit", "*** Add File: code.py",
            "*** Begin Patch\n*** Add File: code.py\ninvalid\n*** End Patch",
            "*** Begin Patch\n*** Move to: code.py\n*** End Patch",
            "*** Begin Patch\n*** Update File: code.py\n*** End Patch",
            "*** Begin Patch\n*** Update File: code.py\n@@\n*** End Patch",
            "*** Begin Patch\n*** Update File: code.py\n*** Move to: first.py\n"
                "*** Move to: second.py\n*** End Patch",
            "*** Begin Patch\n*** Delete File: code.py\n+invalid\n*** End Patch"):
            self.assertEqual(patch_targets(text)[0], (None,))
        self.assertEqual(patch_targets("*** Begin Patch\n*** Add File: code.py\n"
            "+*** Add File: payload.py\n*** End Patch")[0], ("code.py",))
