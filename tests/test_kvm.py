"""Plan selection and KVM admission, with no billable provisioning."""
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from test_envr import envr


class KVMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = envr.Envr(Path(self.temp.name))
        self.api = SimpleNamespace(provider_name='vultr', display_name='Vultr', plan_ids=envr.PLAN_IDS, regions=('fra', 'ams', 'waw'))
        self.plan = envr.KVM_PLAN_IDS['vultr'][8]

    def test_catalog_adds_only_three_exact_local_disk_choices(self):
        catalog = {p: {'id': p, 'ram': size * 1024, 'vcpu_count': 2}
                   for size, p in envr.KVM_PLAN_IDS['vultr'].items()}
        catalog['vc2-4c-8gb'] = {'id': 'vc2-4c-8gb', 'ram': 8192}
        catalog['vx1-g-2c-8g'] = {'id': 'vx1-g-2c-8g', 'ram': 8192}
        self.app.plan_catalog = mock.Mock(return_value=catalog)
        self.app.ubuntu_os_id = mock.Mock()
        self.app.available_regions = mock.Mock(return_value=['ams', 'lhr'])
        options = self.app.resize_options(self.api)
        self.assertEqual(len(options), 4)
        self.assertEqual([o['ram_gb'] for o in options], [8, 8, 16, 32])
        self.assertEqual(options[0]['plan_id'], 'vc2-4c-8gb')
        self.assertEqual(options[1]['plan_id'], self.plan)
        self.assertEqual([o['ram_gb'] for o in options if o['kvm']], [8, 16, 32])
        self.assertNotIn('vx1-g-2c-8g', [o['plan_id'] for o in options])
        self.assertEqual(options[-1]['regions'], ['ams', 'lhr'])

    def test_unavailable_kvm_sizes_are_not_offered(self):
        self.app.plan_catalog = mock.Mock(return_value={self.plan: {'ram': 8192}})
        self.app.ubuntu_os_id = mock.Mock()
        self.app.available_regions = mock.Mock(side_effect=envr.EnvrError('no capacity'))
        self.assertEqual(self.app.resize_options(self.api), [])

    def test_running_but_locked_host_is_not_ready(self):
        locked = {'id': 'target', 'status': 'active', 'power_status': 'running',
                  'server_status': 'locked', 'main_ip': '192.0.2.1'}
        ready = dict(locked, server_status='ok')
        self.api.get = mock.Mock(side_effect=[{'instance': locked}, {'instance': ready}])
        with mock.patch.object(envr.time, 'sleep') as sleep:
            result = self.app.wait_for_instance(self.api, 'target', cache_state=False)
        self.assertEqual(result, ready)
        sleep.assert_called_once_with(5)

    def test_ssh_attempt_timeout_is_retried_within_startup_window(self):
        self.app.remote_command = mock.Mock(side_effect=[
            envr.subprocess.TimeoutExpired(['ssh'], 15), SimpleNamespace(returncode=0)])
        with mock.patch.object(envr.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            self.app.wait_for_ssh({'main_ip': '192.0.2.1'})
        self.assertEqual(self.app.remote_command.call_count, 2)

    def test_ssh_failure_preserves_diagnostic(self):
        self.app.remote_command = mock.Mock(return_value=SimpleNamespace(
            returncode=255, stderr='Permission denied (publickey).'))
        with mock.patch.object(envr.time, 'monotonic', side_effect=[0, 0, 601]), \
                mock.patch.object(envr.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(envr.EnvrError, 'Permission denied'):
            self.app.wait_for_ssh({'main_ip': '192.0.2.1'})

    def test_kvm_regions_follow_live_availability(self):
        self.api.get = mock.Mock(side_effect=lambda path: {'available_plans': [self.plan] if '/lhr/' in path else []})
        self.assertEqual(self.app.available_regions(self.api, self.plan, {'locations': ['ams','lhr','ewr']}), ['lhr'])

    def test_marker_retains_plan_after_process_restart(self):
        self.app.remote_script = mock.Mock()
        self.app.set_resize_marker({'id': 'source'}, 8, plan_id=self.plan)
        self.assertEqual(self.app.remote_script.call_args.args[2], ('source', '8', self.plan))
        self.app._resize_markers.clear()
        self.app._resize_plan_ids.clear()
        self.app.remote_command = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='source:8:' + self.plan))
        self.assertEqual(self.app.resize_marker_state({'id': 'source'}), ('source', 8))
        self.assertEqual(self.app._resize_plan_ids['source'], self.plan)

    def test_same_size_menu_passes_selected_family(self):
        source = {'id': 'source', 'provider': 'vultr', 'ram': 8192, 'plan': 'vc2-4c-8gb', 'region': 'fra'}
        self.app.save_instance_state(source, ram_gb=8)
        self.app.api = mock.Mock(return_value=self.api)
        self.app.resize_roles = mock.Mock(return_value=([source], [], []))
        self.app.ensure_device_access = mock.Mock()
        self.app.resize_marker_state = mock.Mock(return_value=None)
        self.app.plan_catalog = mock.Mock(return_value={})
        self.app.account_response = mock.Mock()
        self.app.resize_options = mock.Mock(return_value=[{'ram_gb': 8, 'plan_id': self.plan, 'vcpu_count': 2, 'region': 'ams', 'plan': {}, 'kvm': 'KVM · VX1'}])
        self.app.resize = mock.Mock()
        with mock.patch('builtins.input', side_effect=['1','1']), contextlib.redirect_stdout(io.StringIO()):
            self.app.resize_menu()
        self.app.resize.assert_called_once_with(8, plan_id=self.plan)

    def test_kvm_failure_prevents_target_validation_and_source_promotion(self):
        self.app.remote_script = mock.Mock(return_value=SimpleNamespace(returncode=0))
        self.app.remote_command = mock.Mock(return_value=SimpleNamespace(returncode=1))
        self.app.sync_from_server = mock.Mock()
        with self.assertRaisesRegex(envr.EnvrError, 'KVM is not usable'):
            self.app.validate_resize_target({'id': 'target', 'plan': self.plan, 'provider': 'vultr'})
        self.app.sync_from_server.assert_not_called()

    def test_other_plans_do_not_change_kvm_configuration(self):
        self.app.remote_script = mock.Mock()
        self.app.ensure_kvm({'plan': 'vc2-4c-8gb'})
        self.app.remote_script.assert_not_called()
        self.assertEqual(envr.kvm_label('exoscale', 'standard.large'), 'KVM experimental')
        self.assertEqual(envr.kvm_label('upcloud', '4xCPU-8GB'), '')

    def test_kvm_probe_uses_new_login_after_group_change(self):
        self.app.remote_script = mock.Mock(return_value=SimpleNamespace(returncode=0))
        self.app.remote_command = mock.Mock(return_value=SimpleNamespace(returncode=0))
        with contextlib.redirect_stdout(io.StringIO()):
            self.app.ensure_kvm({'plan': self.plan})
        self.assertTrue(self.app.remote_command.call_args.kwargs['fresh_connection'])

    def test_fresh_login_disables_connection_reuse(self):
        with mock.patch.object(envr, 'run') as run:
            self.app.remote_command({'main_ip': '192.0.2.1'}, 'true', fresh_connection=True)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ['ssh', '-o', 'ControlMaster=no', '-o', 'ControlPath=none'])

    def test_creation_uses_selected_plan_and_checks_kvm(self):
        a = self.app
        a.plan_catalog = mock.Mock(return_value={self.plan: {'id': self.plan, 'ram': 8192}})
        a.ubuntu_os_id = mock.Mock(return_value=2284)
        a.available_regions = mock.Mock(return_value=['ams'])
        a.ensure_vps_key = mock.Mock(return_value='ssh-ed25519 TEST')
        a.ensure_vultr_ssh_key = mock.Mock(return_value='key')
        a.managed_vultr_ssh_keys = mock.Mock(return_value=[])
        self.api.post = mock.Mock(return_value={'instance': {'id': 'new'}})
        active = {'id': 'new', 'ram': 8192, 'plan': self.plan, 'provider': 'vultr'}
        a.wait_for_instance = mock.Mock(return_value=active)
        a.wait_for_ssh = mock.Mock()
        a.wait_for_cloud_init = mock.Mock()
        a.ensure_kvm = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            a.create_instance(self.api, 8, plan_id=self.plan, cache_state=False, prepare_workspace=False)
        self.assertEqual(self.api.post.call_args.args[1]['plan'], self.plan)
        a.ensure_kvm.assert_called_once_with(active)

    def test_same_size_resize_reaches_confirmation_for_different_plan(self):
        a = self.app
        source = {'id': 'source', 'provider': 'vultr', 'ram': 8192, 'plan': 'vc2-4c-8gb'}
        a.save_instance_state(source, ram_gb=8)
        a.selected_provider = mock.Mock(return_value='vultr')
        a.api = mock.Mock(return_value=self.api)
        a.account_response = mock.Mock()
        a.resize_roles = mock.Mock(return_value=([source], [], []))
        a.ensure_device_access = mock.Mock()
        a.wait_for_cloud_init = mock.Mock()
        a.ensure_resize_source_ready = mock.Mock()
        a.create_instance = mock.Mock()
        with mock.patch.object(envr, 'ensure_commands'), mock.patch.object(envr, 'prompt_yes_no', return_value=False) as ask, contextlib.redirect_stdout(io.StringIO()):
            a.resize(8, plan_id=self.plan)
        ask.assert_called_once()
        a.create_instance.assert_not_called()

    def test_migration_resume_does_not_fall_back_to_standard_plan(self):
        a = self.app
        source = {'id': 'source', 'provider': 'vultr', 'ram': 8192, 'plan': 'vc2-4c-8gb'}
        a.save_instance_state(source, ram_gb=8)
        a.selected_provider = mock.Mock(return_value='vultr')
        a.api = mock.Mock(return_value=self.api)
        a.resize_roles = mock.Mock(return_value=([source], [], []))
        a.ensure_device_access = mock.Mock()
        a.remote_command = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='source:8:' + self.plan))
        a.resize = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            a.resize_menu()
        a.resize.assert_called_once_with(8, plan_id=self.plan)
