# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from functools import partial
import json
from os import listdir
from os.path import join
import re
import unittest
from crashclouseau import java


class JavaTest(unittest.TestCase):

    def readfile(self, filename):
        with open(filename, 'r') as In:
            return json.load(In)

    def get_files(self, path):
        pat = re.compile(r'stack\.[0-9]+\.json')
        for f in listdir(path):
            if pat.match(f):
                full = join(path, f)
                yield full

    @staticmethod
    def get_full_path(java_files, filename):
        pat = re.compile(r'.*/' + filename)
        for f in java_files:
            if pat.match(f):
                return f

    def test(self):
        java_files = java.get_all_java_files()
        for f in self.get_files('./tests/java'):
            data = self.readfile(f)
            stack, files = java.inspect_java_stacktrace(data['stack'],
                                                        'tip',
                                                        get_full_path=partial(JavaTest.get_full_path,
                                                                              java_files))
            self.assertEqual(stack, data['frames'])
            self.assertEqual(list(sorted(files)), data['files'])

            reformatted = java.reformat_java_stacktrace(data['stack'],
                                                        data['channel'],
                                                        data['buildid'],
                                                        get_full_path=partial(JavaTest.get_full_path,
                                                                              java_files),
                                                        get_changeset=lambda x, y, z: None)
            self.assertEqual(reformatted, data['reformatted'])
