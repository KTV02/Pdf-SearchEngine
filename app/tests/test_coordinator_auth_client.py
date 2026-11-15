import importlib.util, os, pathlib, tempfile, unittest
from unittest.mock import patch, Mock

module_path=pathlib.Path(__file__).resolve().parents[1]/'backend/job_coordinator.py'
spec=importlib.util.spec_from_file_location('auth_client_fixture',module_path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class AuthenticatedClient(unittest.TestCase):
    def client(self,**kwargs):return module.NodeRedJobCoordinator(base_url='http://coordinator.invalid',auth_token='fixture-service-token',**kwargs)
    def response(self):
        r=Mock(status_code=200,text='{}');r.json.return_value={'ok':True,'statusCode':200};return r
    def test_private_file_loaded(self):
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('fixture-service-token\n');f.flush()
            with patch.dict(os.environ,{'NODERED_COORDINATOR_URL':'http://coordinator.invalid','NODERED_COORDINATOR_TOKEN_FILE':f.name},clear=True):
                self.assertEqual(module.NodeRedJobCoordinator.from_env().auth_token,'fixture-service-token')
    def test_missing_file_does_not_silently_drop_auth(self):
        with patch.dict(os.environ,{'NODERED_COORDINATOR_TOKEN_FILE':'/not-existing-fixture-token'},clear=True):
            with self.assertRaises(FileNotFoundError):module.NodeRedJobCoordinator.from_env()
    def test_invalid_token_rejected(self):
        for token in ['', 'abc def', 'x'*4097]:
            with tempfile.NamedTemporaryFile(mode='w') as f:
                f.write(token);f.flush()
                with patch.dict(os.environ,{'NODERED_COORDINATOR_TOKEN_FILE':f.name},clear=True):
                    with self.assertRaises(ValueError):module.NodeRedJobCoordinator.from_env()
    def test_request_uses_header_not_url(self):
        with patch.object(module.requests,'request',return_value=self.response()) as send:
            self.client()._request_json('GET','/coordinator/health')
            args=send.call_args;self.assertNotIn('fixture-service-token',str(args.args));self.assertEqual(args.kwargs['headers']['Authorization'],'Bearer fixture-service-token');self.assertFalse(args.kwargs['allow_redirects'])
    def test_custom_headers_preserved_without_overriding_auth(self):
        with patch.object(module.requests,'request',return_value=self.response()) as send:
            self.client()._request_json('GET','/coordinator/health',headers={'X-Fixture':'yes','Authorization':'wrong'})
            self.assertEqual(send.call_args.kwargs['headers'],{'X-Fixture':'yes','Authorization':'Bearer fixture-service-token'})
    def test_get_status_authenticated(self):
        with patch.object(module.requests,'get',return_value=self.response()) as send:
            self.client().get_status();self.assertEqual(send.call_args.kwargs['headers']['Authorization'],'Bearer fixture-service-token')
    def test_notify_authenticated(self):
        with patch.object(module.requests,'post',return_value=self.response()) as send:
            self.assertTrue(self.client(action_endpoint='/coordinator/jobs/heartbeat').notify('fixture'))
            self.assertEqual(send.call_args.kwargs['headers']['Authorization'],'Bearer fixture-service-token')
    def test_authentication_rejection_not_treated_as_ok_json(self):
        response=self.response();response.status_code=401;response.raise_for_status.side_effect=module.requests.HTTPError('fixture 401')
        with patch.object(module.requests,'request',return_value=response):
            with self.assertRaises(module.requests.HTTPError):self.client()._request_json('GET','/coordinator/health')
    def test_legacy_disabled_fixture_requires_no_token(self):
        with patch.dict(os.environ,{},clear=True):self.assertFalse(module.NodeRedJobCoordinator.from_env().enabled)
    def test_no_auth_keeps_existing_mock_api_compatible(self):
        with patch.object(module.requests,'request',return_value=self.response()) as send:
            module.NodeRedJobCoordinator(base_url='http://coordinator.invalid')._request_json('GET','/coordinator/health')
            self.assertNotIn('headers',send.call_args.kwargs)
