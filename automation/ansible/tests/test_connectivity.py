"""Exercise transport probes and scoped Controller jobs without cloud resources."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import requests

spec=importlib.util.spec_from_file_location('aia_connectivity',Path(__file__).parents[1]/'plugins/modules/aia_connectivity.py')
network=importlib.util.module_from_spec(spec);spec.loader.exec_module(network)


@unittest.skipUnless(shutil.which('openssl'),'openssl required for a local TLS fixture')
class TransportProbe(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'key'),'-out',str(root/'cert'),'-days','1','-subj','/CN=localhost'],check=True,capture_output=True)
        fixture=self;self.http_status=200;self.response_delay=0
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                time.sleep(fixture.response_delay)
                self.send_response(fixture.http_status);self.end_headers()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/'cert',root/'key');self.server.socket=context.wrap_socket(self.server.socket,server_side=True)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.target=network.https_target('service','https://127.0.0.1:'+str(self.server.server_port))

    def probe(self,target=None,**kwargs):
        return network.local_probe(dict(targets=[target or self.target],timeout=.5,require_all_addresses=True,validate_certs=False,**kwargs))

    def test_managed_web_503_is_transport_ready_but_shared_503_is_not(self):
        self.http_status=503
        self.assertEqual(self.probe()['status'],'failed')
        result=self.probe(dict(self.target,allow_503=True))
        self.assertEqual(result['status'],'passed');self.assertEqual(result['targets'][0]['http_status'],503)

    def test_certificate_validation_is_not_silently_disabled(self):
        result=network.local_probe(dict(targets=[self.target],timeout=.5,require_all_addresses=True,validate_certs=True))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['targets'][0]['tls_http_error'],'SSLCertVerificationError')

    def test_http_response_can_outlast_the_tcp_connect_deadline(self):
        self.http_status=503;self.response_delay=.75
        result=self.probe(dict(self.target,allow_503=True))
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['targets'][0]['http_status'],503)

    def test_partial_dns_address_failure_is_retained_and_fails_strict_gate(self):
        port=self.server.server_port;real=socket.getaddrinfo
        def resolve(host,*args,**kw):
            if host=='localhost':return [(2,1,6,'',('127.0.0.1',port)),(2,1,6,'',('127.0.0.2',port))]
            return real(host,*args,**kw)
        with patch.object(socket,'getaddrinfo',side_effect=resolve):result=self.probe(dict(self.target,host='localhost'))
        target=result['targets'][0];self.assertTrue(target['reachable']);self.assertTrue(target['partial']);self.assertEqual(result['status'],'failed')
        self.assertEqual(sum(a['connected'] for a in target['addresses']),1)

    def test_credential_bearing_endpoint_is_rejected(self):
        with self.assertRaises(network.ReadinessError):network.https_target('unsafe','https://user:secret@example.test')


class ControllerProbe(unittest.TestCase):
    def setUp(self):
        fixture=self;self.writes=[];self.jobs={};self.next_id=10;self.command_passes=True;self.ping_passes=True;self.running=False;self.event_reads=0;self.empty_event_reads=0
        self.node=dict(host='ssh.student.example.test',port=30222,username='lab-user',password='synthetic-node-secret')
        self.probe=dict(status='passed',hostname='execution-pod',targets=[dict(name='managed_ssh',reachable=True,status='passed')])
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def handle_request(self):
                parsed=urlsplit(self.path);parts=parsed.path.removeprefix('/api/controller/v2/').strip('/').split('/');resource=parts[0];code=200
                params={k:v[0] for k,v in parse_qs(parsed.query).items()};body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
                if self.command=='POST':
                    fixture.writes.append((self.path,body))
                    if len(parts)>2:
                        fixture.jobs[int(parts[1])]['status']='canceled';result={}
                    else:
                        fixture.next_id+=1;identifier=fixture.next_id
                        successful=fixture.command_passes if body['module_name']=='command' else fixture.ping_passes
                        status='running' if fixture.running else ('successful' if successful else 'failed')
                        result=dict(id=identifier,status=status,**body);fixture.jobs[identifier]=copy.deepcopy(result);code=201
                elif resource=='ad_hoc_commands':
                    job=fixture.jobs[int(parts[1])]
                    if len(parts)==2:result=job
                    else:
                        if job['module_name']=='command':res=dict(stdout=json.dumps(fixture.probe))
                        else:res=dict(ping='pong') if fixture.ping_passes else dict(msg='SSH Connection timed out')
                        result=dict(results=[dict(event_data=dict(res=res))],next=None)
                        fixture.event_reads+=1
                        if fixture.event_reads<=fixture.empty_event_reads:result['results']=[]
                else:
                    values={
                        'me':[dict(username='instructor-one')],
                        'organizations':[dict(id=1,name='instructor-one')],
                        'inventories':[dict(id=2,name='Island Adventure',organization=1)],
                        'credentials':[dict(id=3,name='Managed Node SSH',organization=1)],
                        'execution_environments':[dict(id=4,name='Default execution environment')],
                        'hosts':[dict(id=5,name='rhel',inventory=2,variables=json.dumps(dict(ansible_host=fixture.node['host'],ansible_port=fixture.node['port'])))],
                    }[resource]
                    result=dict(results=[v for v in values if all(str(v.get(k))==value for k,value in params.items())],next=None)
                self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(result).encode())
            do_GET=do_POST=handle_request
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.url=f'http://127.0.0.1:{server.server_port}';self.session=requests.Session();self.addCleanup(self.session.close)
        self.api=network.Controller(self.session,self.url,poll=0)

    def test_scoped_probe_and_ping_do_not_send_passwords_or_prohibited_extra_vars(self):
        resources=self.api.resources('instructor-one',self.node)
        result=self.api.run(resources,'command','localhost','python3 safe-probe')
        self.assertEqual(result['status'],'passed');self.assertEqual(result['probe'],self.probe)
        self.assertEqual(self.api.run(resources,'ping','rhel')['status'],'passed')
        self.assertNotIn('extra_vars',self.writes[0][1]);self.assertNotIn(self.node['password'],json.dumps(self.writes))
        self.assertEqual(self.writes[0][1]['inventory'],2);self.assertEqual(self.writes[0][1]['credential'],3)

    def test_network_failure_json_survives_a_failed_aap_command(self):
        self.command_passes=False;self.probe=dict(status='failed',targets=[dict(name='managed_ssh',reachable=False,status='failed')])
        result=self.api.run(dict(inventory=2,credential=3,execution_environment=4),'command','localhost')
        self.assertEqual(result['status'],'failed');self.assertEqual(result['probe'],self.probe)

    def test_terminal_job_waits_for_delayed_probe_events_without_relaunching(self):
        self.empty_event_reads=2
        with patch.object(network.time,'sleep'):
            result=self.api.run(dict(inventory=2,credential=3,execution_environment=4),'command','localhost')
        self.assertEqual(result['status'],'passed');self.assertEqual(self.event_reads,3)
        self.assertEqual(len(self.writes),1)

    def test_terminal_ping_waits_for_delayed_events(self):
        self.empty_event_reads=1
        with patch.object(network.time,'sleep'):
            result=self.api.run(dict(inventory=2,credential=3,execution_environment=4),'ping','rhel')
        self.assertEqual(result['status'],'passed');self.assertEqual(self.event_reads,2)
        self.assertEqual(len(self.writes),1)

    def test_missing_probe_events_remain_a_failure_after_bounded_retries(self):
        self.empty_event_reads=20
        with patch.object(network.time,'sleep'):
            result=self.api.run(dict(inventory=2,credential=3,execution_environment=4),'command','localhost')
        self.assertEqual(result['error'],'aap_probe_output_missing');self.assertEqual(self.event_reads,10)
        self.assertEqual(len(self.writes),1)

    def test_failed_aap_execution_cannot_pass_using_partial_success_output(self):
        self.command_passes=False
        result=self.api.run(dict(inventory=2,credential=3,execution_environment=4),'command','localhost')
        self.assertEqual(result['status'],'failed');self.assertEqual(result['error'],'aap_probe_job_not_successful')
        self.assertEqual(result['probe']['status'],'passed')

    def test_failed_ping_is_classified_without_returning_raw_error_output(self):
        self.ping_passes=False;result=self.api.run(dict(inventory=2,credential=3,execution_environment=4),'ping','rhel')
        self.assertEqual(result['error'],'ssh_timeout');self.assertNotIn('msg',result)

    def test_timeout_cancels_only_the_command_launched_by_this_probe(self):
        self.running=True;self.api.timeout=0;result=self.api.run(dict(inventory=2,credential=3,execution_environment=4),'ping','rhel')
        self.assertEqual(result['error'],'aap_probe_timeout');self.assertEqual(self.writes[-1][0],'/api/controller/v2/ad_hoc_commands/11/cancel/')

    def test_wrong_instructor_is_rejected_before_creating_a_job(self):
        with self.assertRaises(network.ReadinessError):self.api.resources('instructor-two',self.node)
        self.assertEqual(self.writes,[])

    def test_unreachable_aap_tcp_path_skips_slow_ssh_but_keeps_reverse_evidence(self):
        self.command_passes=False;self.probe=dict(status='failed',targets=[dict(name='managed_ssh',reachable=False,status='failed')])
        control=dict(status='passed',targets=[dict(name='managed_ssh',addresses=[dict(address='127.0.0.1',connected=True)])])
        with patch.object(network,'local_probe',return_value=control), patch.object(network,'Controller',return_value=self.api), patch.object(network,'reverse_probe',return_value=dict(status='failed',targets=[])) as reverse:
            report=network.measure(self.session,'https://aap.example.test','instructor-one','one',self.node,[])
        self.assertEqual(report['status'],'failed');self.assertEqual(report['sources']['aap_ssh']['status'],'blocked');reverse.assert_called_once()
        self.assertEqual(len(self.writes),1);self.assertNotIn(self.node['password'],json.dumps(report))

    def test_aap_job_reference_survives_a_polling_network_error(self):
        control=dict(status='passed',targets=[dict(name='managed_ssh',addresses=[dict(address='127.0.0.1',connected=True)])])
        self.running=True;real=self.api.api
        def intermittent(method,path,*args,**kwargs):
            if method=='GET' and path=='ad_hoc_commands/11/':raise requests.ReadTimeout()
            return real(method,path,*args,**kwargs)
        with patch.object(network,'local_probe',return_value=control), patch.object(network,'Controller',return_value=self.api), patch.object(network,'reverse_probe',return_value=dict(status='passed',targets=[])), patch.object(self.api,'api',side_effect=intermittent):
            report=network.measure(self.session,'https://aap.example.test','instructor-one','one',self.node,[])
        self.assertEqual(report['status'],'failed');self.assertEqual(report['aap_commands'][0]['id'],11)
        self.assertIn('/ad_hoc_commands/11/',report['aap_commands'][0]['url'])
