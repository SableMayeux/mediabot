import json
import unittest
from unittest.mock import patch, Mock
from scripts import external_watchdog as watchdog


class WatchdogTests(unittest.TestCase):
    def test_html_or_unexpected_json_is_not_a_healthy_service(self):
        for body, kind in [(b'<html>Login</html>','media'),(b'[]','requests'),(b'{"version":"1"}','requests')]:
            response = Mock(status=200)
            response.read.return_value=body
            response.__enter__=Mock(return_value=response)
            response.__exit__=Mock(return_value=False)
            with patch.object(watchdog.urllib.request,'urlopen',return_value=response):
                self.assertEqual(watchdog.probe('https://example.com/health',kind),body.startswith(b'{'))

    def test_credentials_and_non_https_urls_are_rejected(self):
        for url in ['http://example.com','https://token@example.com/health']:
            with self.assertRaises(ValueError):watchdog.probe(url,'media')

    def test_one_incident_is_opened_then_reused_and_closed_on_recovery(self):
        env={'MEDIA_HEALTH_URL':'https://example.com/health','REQUESTS_HEALTH_URL':'https://example.com/status'}
        title='[Homelab watchdog] Public media services are unreachable'
        for existing, healthy, action in [([],False,'create'),([{'number':7,'title':title}],False,None),([{'number':7,'title':title}],True,'close')]:
            fake_gh=Mock(side_effect=[json.dumps(existing),'ok'])
            with patch.dict(watchdog.os.environ,env),patch.object(watchdog,'probe',return_value=healthy),patch.object(watchdog,'gh',fake_gh):
                watchdog.main()
            self.assertEqual(fake_gh.call_count,2 if action else 1)
            if action:self.assertEqual(fake_gh.call_args.args[:2],('issue',action))
