"""Shared skill uploads and both CLI integrations; no real accounts or model calls."""
import copy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

import test_core  # isolated app fixtures
import app
import core
import site_skills
from fastapi.testclient import TestClient


SKILL=b'---\nname: demo\ndescription: >\n  A shared skill\n  for both networks\n---\n# Instructions\n'


def archive(entries):
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:
        for name,data in entries:z.writestr(name,data)
    return buffer.getvalue()


class SharedSkills(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        for key,value in [('DATA',str(self.root/'data')),('PLUGIN',str(self.root/'plugin')),
                          ('SYNCED',str(self.root/'account')),('CODEX_HOST_AUTH',str(self.root/'auth.json'))]:
            p=patch.object(core,key,value);p.start();self.addCleanup(p.stop)
        Path(core.CODEX_HOST_AUTH).write_text('{}')
        self.folder=Path(core.PLUGIN)/'skills'
        self.previous=dict(app.app.dependency_overrides)
        self.addCleanup(self.restore_dependencies)
        app.app.dependency_overrides[app.current_user]=lambda:{'id':1,'is_admin':True}
        self.client=TestClient(app.app);self.addCleanup(self.client.close)

    def restore_dependencies(self):
        app.app.dependency_overrides.clear();app.app.dependency_overrides.update(self.previous)

    def test_shared_markdown_upload_and_all_network_names(self):
        response=self.client.post('/api/skills',files={'file':('SKILL.md',SKILL,'text/markdown')})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['site'],{'demo':'A shared skill for both networks'})
        self.assertEqual(response.json()['networks'],['Claude','Codex'])
        self.assertEqual((self.folder/'demo/SKILL.md').read_bytes(),SKILL)
        self.assertEqual(self.client.get('/api/skills').json()['site'],core.skills())

    def test_zip_keeps_resources_and_executable_bits_without_running_them(self):
        script=zipfile.ZipInfo('package/scripts/work.sh');script.external_attr=(stat.S_IFREG|0o755)<<16
        raw=archive([('package/SKILL.md',SKILL),(script,b'#!/bin/sh\nexit 9\n'),('package/assets/data.txt',b'asset'),('README.md',b'outside')])
        site_skills.install(self.folder,'demo.zip',raw)
        self.assertEqual((self.folder/'demo/assets/data.txt').read_bytes(),b'asset')
        self.assertEqual((self.folder/'demo/scripts/work.sh').stat().st_mode&0o777,0o755)
        self.assertFalse((self.folder/'demo/README.md').exists())

    def test_invalid_archives_do_not_escape_or_leave_partial_skills(self):
        symlink=zipfile.ZipInfo('demo/link');symlink.external_attr=(stat.S_IFLNK|0o777)<<16
        bad=[archive([('../outside',b'bad'),('demo/SKILL.md',SKILL)]),
             archive([('/tmp/outside',b'bad'),('demo/SKILL.md',SKILL)]),
             archive([('demo\\escape',b'bad'),('demo/SKILL.md',SKILL)]),
             archive([(symlink,b'/etc/passwd'),('demo/SKILL.md',SKILL)]),
             archive([('one/SKILL.md',SKILL),('two/SKILL.md',SKILL)]),b'not a zip']
        for raw in bad:
            with self.subTest(raw=raw[:25]),self.assertRaises(ValueError):
                site_skills.install(self.folder,'bad.zip',raw)
        self.assertFalse(self.folder.exists())

    def test_size_limits_invalid_metadata_and_no_overwrite(self):
        site_skills.install(self.folder,'SKILL.md',SKILL)
        with self.assertRaises(FileExistsError):site_skills.install(self.folder,'SKILL.md',SKILL+b'changed')
        self.assertEqual((self.folder/'demo/SKILL.md').read_bytes(),SKILL)
        for raw in [b'no metadata',SKILL.replace(b'name: demo',b'name: ../escape'),SKILL.replace(b'description: >',b'other: >')]:
            with self.assertRaises(ValueError):site_skills.install(self.folder,'bad.md',raw)
        with patch.object(site_skills,'MAX_UNPACKED',10),self.assertRaises(ValueError):
            site_skills.install(self.folder,'big.zip',archive([('SKILL.md',SKILL)]))
        self.assertEqual(list(self.folder.iterdir()),[self.folder/'demo'])

    def test_upload_is_admin_only_and_errors_are_actionable(self):
        app.app.dependency_overrides[app.current_user]=lambda:{'id':2,'is_admin':False}
        self.assertEqual(self.client.get('/api/skills').status_code,403)
        self.assertEqual(self.client.post('/api/skills',files={'file':('SKILL.md',SKILL)}).status_code,403)
        app.app.dependency_overrides[app.current_user]=lambda:{'id':1,'is_admin':True}
        self.assertEqual(self.client.post('/api/skills',files={'file':('bad.md',b'wrong')}).status_code,400)
        self.assertEqual(self.client.post('/api/skills',files={'file':('demo.md',SKILL)}).status_code,200)
        self.assertEqual(self.client.post('/api/skills',files={'file':('demo.md',SKILL)}).status_code,409)
        with patch.object(site_skills,'MAX_UPLOAD',5):
            self.assertEqual(self.client.post('/api/skills',files={'file':('big.md',SKILL)}).status_code,413)

    def test_one_folder_is_connected_to_both_networks_and_permissions_still_apply(self):
        site_skills.install(self.folder,'demo.md',SKILL)
        uid=3;meta={'id':uuid.uuid4().hex,'sid':str(uuid.uuid4()),'model':'sonnet','effort':''}
        cwd=core.workspace(uid,meta['id'])
        access=copy.deepcopy(test_core.ALL)
        args=core.claude_args(uid,meta,cwd,access)
        self.assertEqual(args[args.index('--plugin-dir')+1],core.PLUGIN)
        sandbox=json.loads(args[args.index('--settings')+1])['sandbox']['filesystem']
        self.assertIn(core.PLUGIN,sandbox['allowRead'])
        self.assertIn(core.PLUGIN,sandbox['denyWrite'])
        linked=core.codex_skills(uid)
        self.assertEqual(linked,[str(self.folder/'demo')])
        link=Path(core.codex_home(uid))/'skills/demo'
        self.assertEqual(link.resolve(),self.folder/'demo')
        core.codex_skills(uid,disabled=['demo'])
        self.assertFalse(link.exists())
        self.assertTrue((self.folder/'demo/SKILL.md').exists())
        access['skills']=False
        self.assertIn('--safe-mode',core.claude_args(uid,meta,cwd,access))

    def test_unpublishing_account_link_cleans_codex_link_but_preserves_native_skills(self):
        source=Path(core.SYNCED)/'bucket/demo';source.mkdir(parents=True)
        (source/'SKILL.md').write_bytes(SKILL)
        core.publish_skills({'demo'});core.codex_skills(3)
        folder=Path(core.codex_home(3))/'skills'
        (folder/'.system').mkdir()
        (folder/'native').symlink_to(source)
        core.publish_skills(set());core.codex_skills(3)
        self.assertFalse(os.path.lexists(folder/'demo'))
        self.assertTrue((folder/'.system').is_dir())
        self.assertTrue((folder/'native').is_symlink())
