import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('watch_race',Path(__file__).resolve().parents[1]/'bin/gpu-watch.py')
W=importlib.util.module_from_spec(spec);spec.loader.exec_module(W)


class OwnershipRaceTests(unittest.TestCase):
    def test_exact_current_cgroup_component(self):
        identity='a'*64
        for path, expected in [('/system.slice/docker-'+identity+'.scope',True),('/docker/'+identity,True),
                               ('/docker/'+identity+'0',False),('/docker/'+('b'*64),False)]:
            with self.subTest(path=path), patch.object(W.Path,'read_text',return_value='0::'+path+'\n'):
                self.assertEqual(W.gpu_pid_is_owned(71,identity),expected)

    def test_new_own_runner_is_not_foreign(self):
        with patch.object(W,'gpu_compute_pids',return_value={71}),patch.object(W,'gpu_pid_is_owned',return_value=True) as owned:
            self.assertEqual(W.foreign_compute_pids('current'),set())
            owned.assert_called_once_with(71,'current')

    def test_actual_foreign_process_remains_foreign(self):
        with patch.object(W,'gpu_compute_pids',return_value={71}),patch.object(W,'gpu_pid_is_owned',return_value=False):
            self.assertEqual(W.foreign_compute_pids('current'),{71})

    def test_exited_runner_disappears_on_fresh_gpu_query(self):
        with patch.object(W,'gpu_compute_pids',side_effect=[{71},set()]) as query,patch.object(W,'gpu_pid_is_owned',side_effect=FileNotFoundError):
            self.assertEqual(W.foreign_compute_pids('current'),set())
            self.assertEqual(query.call_count,2)

    def test_reused_pid_gets_fresh_ownership_check(self):
        with patch.object(W,'gpu_compute_pids',side_effect=[{71},{71}]),patch.object(W,'gpu_pid_is_owned',side_effect=[FileNotFoundError(),False]):
            self.assertEqual(W.foreign_compute_pids('current'),{71})

    def test_persistently_unresolvable_gpu_pid_fails_closed(self):
        with patch.object(W,'gpu_compute_pids',side_effect=[{71},{71}]),patch.object(W,'gpu_pid_is_owned',side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError,'unresolved_gpu_process'):
                W.foreign_compute_pids('current')

    def test_cgroup_access_error_fails_closed(self):
        with patch.object(W,'gpu_compute_pids',return_value={71}),patch.object(W,'gpu_pid_is_owned',side_effect=PermissionError):
            with self.assertRaises(PermissionError):W.foreign_compute_pids('current')


if __name__=='__main__':unittest.main()
