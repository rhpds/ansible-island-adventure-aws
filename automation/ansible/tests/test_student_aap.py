"""Exercise AAP API provisioning, organization isolation, and safe reruns."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs,urlsplit
import requests

spec=importlib.util.spec_from_file_location('aia_aap_student',Path(__file__).parents[1]/'library/aia_aap_student.py')
aap=importlib.util.module_from_spec(spec);spec.loader.exec_module(aap)


class StudentAAP(unittest.TestCase):
    def setUp(self):
        self.resources={k:[] for k in ('inventories','hosts','groups','credentials')}
        self.resources['inventories']=[dict(id=500,name='My existing test',organization=1,description='Keep me')]
        self.members={};self.writes=[];fixture=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def handle_request_data(self):
                parsed=urlsplit(self.path);parts=parsed.path.removeprefix('/api/controller/v2/').strip('/').split('/')
                params={k:v[0] for k,v in parse_qs(parsed.query).items()}
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
                resource=parts[0];code=200;data={}
                if self.command=='GET':
                    if resource=='me':items=[dict(id=1,username='instructor-one')]
                    elif resource=='organizations':items=[dict(id=1,name='instructor-one')]
                    elif resource=='credential_types':items=[dict(id=7,name='Machine',kind='ssh')]
                    elif resource=='groups' and len(parts)==3:
                        items=[h for h in fixture.resources['hosts'] if h['id'] in fixture.members.get(int(parts[1]),[])]
                    else:items=copy.deepcopy(fixture.resources[resource])
                    items=[x for x in items if all(str(x.get(k))==v for k,v in params.items())]
                    for item in items:
                        if 'inputs' in item:item['inputs']['password']='$encrypted$'
                    data=dict(results=items,next=None,count=len(items))
                elif resource=='groups' and len(parts)==3:
                    fixture.members.setdefault(int(parts[1]),[]).append(body['id'])
                    fixture.writes.append((self.command,self.path));code=204
                else:
                    fixture.writes.append((self.command,self.path))
                    if self.command=='POST':
                        data=body|dict(id=100+sum(map(len,fixture.resources.values())))
                        fixture.resources[resource].append(copy.deepcopy(data));code=201
                    else:
                        data=next(x for x in fixture.resources[resource] if x['id']==int(parts[1]));data.update(body)
                self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers()
                if code!=204:self.wfile.write(json.dumps(data).encode())
            do_GET=do_POST=do_PATCH=handle_request_data
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.url=f'http://127.0.0.1:{server.server_port}'
        self.http=requests.Session();self.addCleanup(self.http.close)
        self.node=dict(host='ssh.student.example.test',port=30222,username='lab-user',password='synthetic-node-password')

    def configure(self,**kw):
        return aap.configure(self.http,self.url,kw.pop('username','instructor-one'),'one',self.node,
                             'rhel.sandbox-one-ocp4-cluster.svc.cluster.local',**kw)

    def test_create_web_host_credentials_and_localhost_then_rerun(self):
        result=self.configure();self.assertTrue(result['changed'])
        host=next(h for h in self.resources['hosts'] if h['name']=='rhel')
        variables=json.loads(host['variables'])
        self.assertEqual(variables['ansible_host'],self.node['host'])
        self.assertEqual(variables['ansible_port'],30222)
        self.assertNotIn('password',host['variables'])
        group=self.resources['groups'][0];self.assertIn(host['id'],self.members[group['id']])
        self.assertEqual(self.resources['credentials'][0]['organization'],1)
        self.assertEqual(self.resources['credentials'][0]['inputs']['password'],self.node['password'])
        self.assertEqual(self.resources['inventories'][0]['description'],'Keep me')
        writes=list(self.writes)
        self.assertFalse(self.configure()['changed']);self.assertEqual(writes,self.writes)
        self.assertNotIn(self.node['password'],json.dumps(result))
        localhost=next(h for h in self.resources['hosts'] if h['name']=='localhost')
        self.assertEqual(json.loads(localhost['variables'])['ansible_python_interpreter'],'{{ ansible_playbook_python }}')

    def test_check_mode_does_not_create_anything(self):
        self.assertTrue(self.configure(check=True)['changed']);self.assertEqual(self.writes,[])

    def test_user_and_guid_mismatch_stops_before_mutation(self):
        with self.assertRaises(aap.ConfigurationError):self.configure(username='instructor-two')
        self.assertEqual(self.writes,[])

    def test_host_customizations_retained_and_secret_rotation_explicit(self):
        self.configure();host=next(h for h in self.resources['hosts'] if h['name']=='rhel')
        variables=json.loads(host['variables']);variables['custom_setting']='retained';host['variables']=json.dumps(variables,sort_keys=True)
        self.assertFalse(self.configure()['changed'])
        self.assertEqual(json.loads(host['variables'])['custom_setting'],'retained')
        self.assertTrue(self.configure(rotate=True)['changed'])

    def test_unrelated_credential_name_collision_is_not_overwritten(self):
        self.resources['credentials'].append(dict(id=9,name='Managed Node SSH',organization=1,credential_type=7,description='User owned',inputs={}))
        with self.assertRaises(aap.ConfigurationError):self.configure()
        self.assertEqual(self.resources['credentials'][0]['description'],'User owned')


class SSHAddressFallback(unittest.TestCase):
    def test_timeout_on_first_address_does_not_hide_a_reachable_second_address(self):
        bad,good=Mock(),Mock();bad.connect.side_effect=TimeoutError()
        addresses=[(2,1,6,'',('192.0.2.1',22)),(2,1,6,'',('192.0.2.2',22))]
        with patch.object(aap.socket,'getaddrinfo',return_value=addresses), patch.object(aap.socket,'socket',side_effect=[bad,good]):
            self.assertIs(aap.connect_socket('ssh.example.test',22),good)
        bad.close.assert_called_once();good.close.assert_not_called()

    def test_total_connect_budget_stops_address_iteration(self):
        addresses=[(2,1,6,'',('192.0.2.1',22))]
        with patch.object(aap.socket,'getaddrinfo',return_value=addresses), patch.object(aap.time,'monotonic',side_effect=[0,31]), patch.object(aap.socket,'socket') as create:
            with self.assertRaises(aap.ConfigurationError):aap.connect_socket('ssh.example.test',22)
        create.assert_not_called()
