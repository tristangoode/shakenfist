from shakenfist_ci import base


class TestDiskSpecifications(base.BaseNamespacedTestCase):
    def __init__(self, *args, **kwargs):
        kwargs['namespace_prefix'] = 'diskspecs'
        super(TestDiskSpecifications, self).__init__(*args, **kwargs)

    def test_default(self):
        inst = self.test_client.create_instance(
            'test-default-disk', 1, 1024, None,
            [
                {
                    'size': 8,
                    'base': 'sf://upload/system/debian-11',
                    'type': 'disk'
                }
            ], None, None)

        self.assertIsNotNone(inst['uuid'])
        self._await_instance_ready(inst['uuid'])

        results = self._await_command(inst['uuid'], 'df -h')
        self.assertEqual(0, results['return-code'])
        self.assertEqual('', results['stderr'])
        self.assertTrue('vda' in results['stdout'])
