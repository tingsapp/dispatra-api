import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from app.agents.availability import availability

spec=importlib.util.spec_from_file_location('watchdog',Path(__file__).parents[1]/'scripts/production-watchdog.py')
watchdog=importlib.util.module_from_spec(spec); spec.loader.exec_module(watchdog)
spec2=importlib.util.spec_from_file_location('control',Path(__file__).parents[1]/'scripts/production-control.py')
control=importlib.util.module_from_spec(spec2); spec2.loader.exec_module(control)

class ProductionTests(unittest.TestCase):
    def test_successful_recovery_is_quiet_and_clears_incident(self):
        incidents,due=watchdog.decisions({}, {'api':False},100)
        self.assertEqual(due,[])
        incidents,due=watchdog.decisions(incidents, {'api':True},200)
        self.assertEqual((incidents,due),({},[]))
    def test_failed_recovery_alerts_only_after_grace_and_once(self):
        incidents,due=watchdog.decisions({}, {'api':False},100)
        self.assertFalse(watchdog.decisions(incidents,{'api':False},279)[1])
        incidents,due=watchdog.decisions(incidents,{'api':False},280)
        self.assertEqual(due,['api']); incidents['api']['notified']=True
        self.assertFalse(watchdog.decisions(incidents,{'api':False},900)[1])
    def test_monitor_fails_closed_for_stale_or_missing_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'TECHNICAL_WATCHDOG_STATE':folder+'/state.json'}), patch('app.agents.availability.time.time',return_value=1000):
            self.assertEqual(availability().status_code,503)
            Path(folder+'/state.json').write_text(json.dumps({'ok':True,'at':990,'secret':'never expose'}))
            response=availability(); self.assertEqual(response.status_code,200)
            self.assertEqual(json.loads(response.body),{'status':'ok'})
            Path(folder+'/state.json').write_text(json.dumps({'ok':True,'at':800}))
            self.assertEqual(availability().status_code,503)
            Path(folder+'/state.json').write_text('[]')
            self.assertEqual(availability().status_code,503)
    def test_privileged_helper_refuses_arbitrary_commands_and_paths(self):
        with patch.object(control.subprocess,'run') as run:
            for args in (['start','ssh.service'],['web','../../etc'],['shell','id'],['stop','dispatra-api.service','extra']):
                with self.assertRaises(ValueError): control.main(args)
            run.assert_not_called()
    def test_privileged_helper_only_runs_fixed_service(self):
        with patch.object(control.subprocess,'run') as run:
            control.main(['start','dispatra-api.service'])
            self.assertEqual(run.call_args.args[0],['/usr/bin/systemctl','start','dispatra-api.service'])

class WatchdogCycleTests(unittest.TestCase):
    def test_inactive_service_recovers_quietly_and_active_service_is_never_restarted(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(watchdog,'STATE',Path(folder)), patch.object(watchdog,'SERVICES',('api.service',)), patch.object(watchdog,'http',return_value=True), patch.object(watchdog,'notify') as notify, patch.object(watchdog,'service') as service:
            service.side_effect=lambda name, action='is-active': action=='start'
            with patch.object(watchdog.time,'time',return_value=100): watchdog.cycle()
            service.assert_any_call('api.service','start'); notify.assert_not_called()
            service.reset_mock(); service.return_value=True; service.side_effect=None
            with patch.object(watchdog.time,'time',return_value=140): watchdog.cycle()
            service.assert_called_once_with('api.service'); notify.assert_not_called()
            self.assertTrue(json.loads((Path(folder)/'state.json').read_text())['ok'])

    def test_email_failure_retries_and_success_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(watchdog,'STATE',Path(folder)), patch.object(watchdog,'SERVICES',()), patch.object(watchdog,'http',return_value=False), patch.object(watchdog,'notify') as notify:
            with patch.object(watchdog.time,'time',return_value=100): watchdog.cycle()
            notify.side_effect=OSError('mail unavailable')
            with patch.object(watchdog.time,'time',return_value=300): watchdog.cycle()
            notify.side_effect=None
            with patch.object(watchdog.time,'time',return_value=330): watchdog.cycle()
            with patch.object(watchdog.time,'time',return_value=360): watchdog.cycle()
            self.assertEqual(notify.call_count,2)

    def test_missing_web_assets_fail_the_check(self):
        with patch.object(watchdog,'web',side_effect=[(200,'<script src="/assets/app.js"></script>'),(404,'')]):
            self.assertFalse(watchdog.http('https://dispatra.com/','html'))
        with patch.object(watchdog,'web',return_value=(200,'an error page')):
            self.assertFalse(watchdog.http('https://dispatra.com/','html'))


class ProductionReleaseTests(unittest.TestCase):
    def test_only_tested_web_artifact_replaces_entry_and_old_assets_survive(self):
        from uuid import uuid4
        from app.agents.code_repair import production
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); current=base/'old'; current.mkdir()
            (current/'index.html').write_text('old');(current/'previous.js').write_text('old asset')
            candidate=base/'candidate'; artifact=candidate/'.artifacts/web'; artifact.mkdir(parents=True)
            (artifact/'index.html').write_text('tested');(artifact/'new.js').write_text('tested asset')
            job=base/str(uuid4());job.mkdir()
            installer=production.ProductionInstaller(Path('/opt/dispatra/client'),'client',job)
            with patch.object(production,'RELEASES',base/'releases'),patch.object(production,'CURRENT_WEB',current):
                installer.prepare(candidate)
            result=base/'releases'/job.name/'web'
            self.assertEqual((result/'index.html').read_bytes(),(artifact/'index.html').read_bytes())
            self.assertEqual((result/'new.js').read_text(),'tested asset')
            self.assertEqual((result/'previous.js').read_text(),'old asset')

    def test_web_rollback_restores_previous_release_even_when_source_rollback_fails(self):
        from app.agents.code_repair.production import ProductionInstaller
        from app.agents.code_repair.install import LocalInstaller
        with tempfile.TemporaryDirectory() as directory:
            installer=ProductionInstaller(Path('/opt/dispatra/client'),'client',Path(directory))
            installer.control=Mock()
            with patch.object(LocalInstaller,'rollback',side_effect=RuntimeError('source conflict')):
                with self.assertRaises(RuntimeError):installer.rollback({'web_before':'previous'})
            installer.control.assert_called_once_with('web','previous')

    def test_maintenance_lease_is_removed_on_failure_and_concurrent_release_is_blocked(self):
        from app.agents.code_repair import production
        with tempfile.TemporaryDirectory() as directory, patch.object(production,'WATCHDOG_STATE',Path(directory)):
            installer=production.ProductionInstaller(Path('/opt/dispatra/api'),'api',Path(directory)/'job')
            with self.assertRaises(RuntimeError),installer.maintenance():
                self.assertTrue((Path(directory)/'maintenance.json').is_file())
                with self.assertRaises(BlockingIOError),installer.maintenance():pass
                raise RuntimeError('failed release')
            self.assertFalse((Path(directory)/'maintenance.json').exists())


class PublicBuildConfigTests(unittest.TestCase):
    def test_only_public_map_setting_reaches_test_environment_after_clearenv(self):
        from app.agents.code_repair.sandbox import Sandbox
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'VITE_GOOGLE_MAPS_API_KEY':''}):
            root=Path(directory);client=root/'client';client.mkdir()
            (client/'.env.production').write_text('VITE_GOOGLE_MAPS_API_KEY=public-browser-key\nOPENAI_API_KEY=private-must-not-copy\n')
            runner=Sandbox({'api':root/'api','client':client})
            args=runner.argv(root,'client','suite','',root)
            self.assertIn('public-browser-key',args)
            self.assertNotIn('private-must-not-copy',args)
            self.assertGreater(args.index('VITE_GOOGLE_MAPS_API_KEY'),args.index('--clearenv'))


class ReleaseCoordinationTests(unittest.TestCase):
    def test_watchdog_does_not_start_services_during_a_locked_release(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory, patch.object(watchdog,'STATE',Path(directory)), patch.object(watchdog,'SERVICES',('api.service',)), patch.object(watchdog,'http',return_value=True), patch.object(watchdog,'notify'), patch.object(watchdog,'service',return_value=False) as service:
            with (Path(directory)/'release.lock').open('w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                watchdog.cycle()
                service.assert_called_once_with('api.service')
            self.assertFalse(watchdog.release_in_progress())


class LoopbackTlsTests(unittest.TestCase):
    def test_checks_real_hostname_over_loopback_and_closes_connection(self):
        with patch.object(watchdog.http_client,'HTTPSConnection') as constructor, patch.object(watchdog.socket,'create_connection') as connect, patch.object(watchdog.ssl,'create_default_context') as context:
            connection=constructor.return_value; response=connection.getresponse.return_value
            response.status=200;response.read.return_value=b'page'
            self.assertEqual(watchdog.web('/admin'),(200,'page'))
            constructor.assert_called_once_with('dispatra.com',timeout=8)
            connect.assert_called_once_with(('127.0.0.1',443),timeout=8)
            context.return_value.wrap_socket.assert_called_once_with(connect.return_value,server_hostname='dispatra.com')
            connection.close.assert_called_once()

if __name__=='__main__': unittest.main()
