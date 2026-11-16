import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.request
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bgmtool.api import Client, SafeRedirect
from bgmtool.common import (DEFAULTS, atomic_json, episode_number, guard, lp, read_json,
                           roots_from, safe, save_config, snapshot, unchanged)
from bgmtool.executor import Executor, pause_jellyfin, unlock_stale
from bgmtool.planner import Planner, choose_episode, scan, suggest

class FakeClient:
    def bundle(self,sid):
        sid = int(sid)
        return {'subject':{'id':sid,'type':2,'name':'Test Show','name_cn':'测试作品',
            'date':'2024-01-01','platform':'剧场版' if sid==20 else 'TV','summary':'简介 & <测试>',
            'rating':{'score':8.1,'total':100},'tags':[{'name':'测试'}]},
            'episodes':[{'id':sid*100+n,'ep':n,'sort':n,'type':0,'name':f'Episode {n}',
                         'name_cn':f'章节 {n}','airdate':'2024-01-01'} for n in range(1,4)],
            'persons':[{'relation':'动画制作','name':'Test Studio'}], 'characters':[]}
    def poster(self,subject): return None

class ToolTests(unittest.TestCase):
    def test_unconfigured_roots_do_not_fall_back_to_cwd(self):
        with self.assertRaisesRegex(ValueError, '请先设置剧集和电影目录'):
            roots_from(DEFAULTS)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bgmtool-test-')
        self.base = Path(self.tmp.name).resolve()
        self.shows = self.base/'JPitems'; self.movies = self.base/'Movies'; self.data = self.base/'data'
        for d in (self.shows,self.movies,self.data): d.mkdir()
        self.config = dict(DEFAULTS,shows_root=str(self.shows),movies_root=str(self.movies),stable_seconds=0)
        self.source = self.shows/'Raw Release'
        self.source.mkdir()
        self.files = []
        for name in ('[Team] Test Show [01][1080p].mkv','[Team] Test Show [02][1080p].mkv','[Team] Test Show [NCOP1].mkv',
                     '[Team] Test Show [01][1080p].zh-Hans.ass','release.nfo','scans.txt'):
            p = self.source/name; p.write_bytes(('original-'+name).encode('utf-8')); self.files.append(p)
        self.original = {str(p):snapshot(p) for p in self.files}
    def tearDown(self): self.tmp.cleanup()
    def plan(self,replace=False):
        result = scan(self.config)
        choices = {g['id']:{'subject_id':10,'kind':'show','season':1,'confirmed':True} for g in result['groups']}
        run,p = Planner(self.config,self.data,FakeClient(),lambda s:None).build(result['groups'],choices,replace)
        return Executor(self.config,self.data,run,lambda s:None),p
    def assert_original(self,skip_old=False):
        for p,e in self.original.items():
            if skip_old and Path(p).suffix=='.nfo': continue
            unchanged(p,e)

    def test_preview_never_changes_media(self):
        ex,p = self.plan(); self.assert_original()
        self.assertEqual(p['summary']['episode'],2); self.assertEqual(p['summary']['extra'],1)
        self.assertEqual(p['summary']['companion'],1); self.assertEqual(p['summary']['video_copies'],0)
        self.assertFalse(ex.journal.exists())

    def test_apply_verify_and_rollback(self):
        ex,p = self.plan(); report = ex.apply(); self.assertTrue(report['verified'])
        self.assertFalse(self.source.exists())
        self.assertTrue(any(e['dst'].endswith('.ignore') for e in p['writes']))
        ex.rollback(); self.assert_original()
        self.assertEqual({p.name for p in self.shows.iterdir()},{'Raw Release'})

    def test_old_nfo_deleted_without_backup(self):
        ex,p = self.plan(True); self.assertEqual(len(p['deletions']),1)
        old = (self.source/'release.nfo').read_bytes()
        self.assertFalse(any(old in f.read_bytes() for f in (ex.run/'staged').iterdir()))
        ex.apply(); ex.rollback(); self.assert_original(True)
        self.assertFalse((self.source/'release.nfo').exists())

    def test_source_changed_refuses_before_mutation(self):
        ex,p = self.plan(); self.files[0].write_bytes(b'changed')
        with self.assertRaises(ValueError): ex.apply()
        self.assertTrue(all(f.exists() for f in self.files)); self.assertFalse(ex.journal.exists())

    def test_destination_conflict_refuses(self):
        ex,p = self.plan(); dst = Path(p['moves'][0]['dst']); dst.parent.mkdir(parents=True); dst.write_bytes(b'other-file')
        with self.assertRaises(ValueError): ex.apply()
        self.assert_original(); self.assertEqual(dst.read_bytes(),b'other-file')

    def test_plan_modification_refuses(self):
        ex,p = self.plan(); (ex.run/'plan.json').write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError): Executor(self.config,self.data,ex.run)
        self.assert_original()

    def test_staged_metadata_modification_refuses(self):
        ex,p = self.plan(); Path(p['writes'][0]['src']).write_bytes(b'changed')
        with self.assertRaises(ValueError): ex.apply()
        self.assert_original()

    def partial_moves(self,ex,count):
        ex.preflight(); ex.log(event='start')
        for e in ex.plan['moves'][:count]:
            dst = Path(e['dst']); ex.mkdirs(dst.parent)
            ex.log(event='intent',action='move',src=e['src'],dst=e['dst'])
            os.rename(lp(e['src']),lp(dst)); ex.log(event='done',action='move',src=e['src'],dst=e['dst'])

    def test_resume_after_partial_move(self):
        ex,p = self.plan(); self.partial_moves(ex,1)
        with self.assertRaises(ValueError): ex.apply()
        ex.apply(resume=True); ex.verify(); ex.rollback(); self.assert_original()

    def test_resume_after_partial_metadata_write(self):
        ex,p = self.plan(); self.partial_moves(ex,len(p['moves']))
        e = p['writes'][0]; dst = Path(e['dst']); ex.mkdirs(dst.parent)
        temp = dst.with_name(dst.name+'.'+p['run_id']+'.tmp')
        ex.log(event='intent',action='write',path=str(dst),temp=str(temp),sha256=e['sha256'])
        temp.write_bytes(Path(e['src']).read_bytes()[:17])
        ex.apply(resume=True); ex.verify(); self.assertFalse(temp.exists())
        ex.rollback(); self.assert_original()

    def test_rollback_partial_metadata(self):
        ex,p = self.plan(); self.partial_moves(ex,len(p['moves']))
        e = p['writes'][0]; dst = Path(e['dst']); ex.mkdirs(dst.parent)
        temp = dst.with_name(dst.name+'.'+p['run_id']+'.tmp')
        ex.log(event='intent',action='write',path=str(dst),temp=str(temp),sha256=e['sha256'])
        temp.write_bytes(Path(e['src']).read_bytes()[:19])
        ex.rollback(); self.assert_original(); self.assertFalse(temp.exists())

    def test_modified_metadata_blocks_entire_rollback(self):
        ex,p = self.plan(); ex.apply(); dst = Path(p['writes'][0]['dst'])
        dst.write_bytes(dst.read_bytes()+b'changed')
        with self.assertRaises(ValueError): ex.rollback()
        self.assertFalse(self.files[0].exists()); self.assertTrue(Path(p['moves'][0]['dst']).exists())

    def test_missing_source_no_journal_refuses(self):
        ex,p = self.plan(); e=p['moves'][0]; dst=Path(e['dst']); dst.parent.mkdir(parents=True)
        os.rename(lp(e['src']),lp(dst))
        with self.assertRaises(ValueError): ex.apply(resume=True)

    def test_resume_complete_run_with_replaced_target_nfo(self):
        owner = self.shows/'Existing Show'; owner.mkdir()
        old = owner/'tvshow.nfo'; old.write_text('<tvshow><bangumiid>10</bangumiid><title>Old</title></tvshow>',encoding='utf-8')
        ex,p = self.plan(True); self.assertTrue(any(e['path']==str(old) for e in p['deletions']))
        ex.apply(); new = old.read_bytes(); ex.apply(resume=True)
        self.assertEqual(old.read_bytes(),new); ex.rollback(); self.assertFalse(old.exists())
        self.assert_original(True)

    def test_existing_owner_metadata_preserved_by_default(self):
        owner = self.shows/'Existing Show'; owner.mkdir()
        old = owner/'tvshow.nfo'; old.write_text('<tvshow><bangumiid>10</bangumiid><title>Personal</title></tvshow>',encoding='utf-8')
        before = old.read_bytes(); ex,p=self.plan(); ex.apply()
        self.assertEqual(old.read_bytes(),before); self.assertFalse(p['deletions'])

    def test_scan_is_incremental(self):
        ex,p=self.plan(); ex.apply(); result=scan(self.config)
        self.assertEqual(len(result['groups']),0); self.assertEqual(result['stats']['already_scraped'],3)

    def test_incremental_new_episode_detects_existing_season(self):
        ex,p=self.plan(); ex.apply()
        old_episode=next(Path(e['dst']) for e in p['moves'] if e['role']=='episode')
        added=old_episode.parent/'Test S01E03.mkv'; added.write_bytes(b'new-episode')
        result=scan(self.config); self.assertEqual(len(result['groups']),1)
        g=result['groups'][0]; self.assertEqual(g['hint_subject_id'],10); self.assertEqual(g['hint_root_id'],10)
        run,p2=Planner(self.config,self.data,FakeClient(),lambda s:None).build([g],{g['id']:{'subject_id':10,'root_subject_id':10,'season':1,'confirmed':True}})
        Executor(self.config,self.data,run,lambda s:None).apply()
        self.assertEqual(len(scan(self.config)['groups']),0)

    def test_ignore_directory_not_scanned(self):
        hidden=self.shows/'Hidden'; hidden.mkdir(); (hidden/'.ignore').write_bytes(b''); (hidden/'secret.mkv').write_bytes(b'video')
        self.assertEqual(scan(self.config)['stats']['videos_seen'],3)

    def test_unconfirmed_never_planned(self):
        result=scan(self.config); run,p=Planner(self.config,self.data,FakeClient(),lambda s:None).build(result['groups'],{})
        self.assertFalse(p['moves']); self.assertFalse(p['writes']); self.assertTrue(p['review']); self.assert_original()

    def test_ambiguous_episode_not_guessed(self):
        episodes=[{'id':1,'ep':1,'sort':14,'type':0},{'id':2,'ep':14,'sort':27,'type':0}]
        with self.assertRaises(ValueError): choose_episode(episodes,14)
        self.assertEqual(choose_episode(episodes,14,'sort')['id'],1)

    def test_unidentified_special_stays_in_review(self):
        special=self.source/'[Team] Test Show [OVA].mkv'; special.write_bytes(b'special')
        ex,p=self.plan(); self.assertTrue(any(e.get('src')==str(special) for e in p['review']))
        ex.apply(); self.assertTrue(special.exists())

    def test_zero_stable_seconds_disables_mtime_gate(self):
        # Windows can report a new file's timestamp ahead of the process clock.
        with mock.patch('bgmtool.planner.time.time', return_value=1):
            self.assertEqual(scan(self.config)['stats']['still_downloading'], 0)
            config = dict(self.config, stable_seconds=120)
            self.assertEqual(scan(config)['stats']['still_downloading'], 3)

    def test_range_preserved(self):
        self.files[0].rename(self.source/'Test Show [01-02].mkv')
        ex,p=self.plan(); self.assertTrue(any('S01E01-E02' in e['dst'] for e in p['moves']))

    def test_movie_collection_separated_and_disc_not_planned(self):
        films=self.movies/'Collection'; films.mkdir()
        for name in ('Film A.mkv','Film B.mkv'): (films/name).write_bytes(b'film')
        disc=self.movies/'Disc'/'BDMV'/'STREAM'; disc.mkdir(parents=True); (disc/'00001.m2ts').write_bytes(b'disc')
        result=scan(self.config); groups=[g for g in result['groups'] if g['kind']=='movie']
        self.assertEqual(len(groups),3)
        choices={g['id']:{'subject_id':20,'kind':'movie','confirmed':True} for g in groups}
        run,p=Planner(self.config,self.data,FakeClient(),lambda s:None).build(groups,choices)
        self.assertEqual(p['summary']['movie'],2); self.assertTrue(any('BDMV' in e['reason'] for e in p['review']))
        Executor(self.config,self.data,run,lambda s:None).apply()
        for e in p['moves']:
            if e['role']=='movie': self.assertTrue(Path(e['dst']).name.startswith(Path(e['dst']).parent.name+' - '))

    def test_root_escape_and_root_delete_refused(self):
        roots=roots_from(self.config)
        for path in (self.shows,self.base/'outside',self.shows/'..'/'outside'):
            with self.assertRaises(ValueError): guard(path,roots)

    def test_overlapping_roots_refused(self):
        with self.assertRaises(ValueError): roots_from(dict(self.config,movies_root=str(self.shows)))

    def test_symlink_or_junction_refused(self):
        link=self.shows/'link'
        try: link.symlink_to(self.movies,target_is_directory=True)
        except OSError: self.skipTest('Symlink privilege unavailable')
        with self.assertRaises(ValueError): guard(link/'video.mkv',roots_from(self.config))

    def test_config_never_saves_token(self):
        save_config(self.data,dict(self.config,token='PRIVATE',cookies='PRIVATE'))
        self.assertNotIn('PRIVATE',(self.data/'config.json').read_text(encoding='utf-8'))

    def test_proxy_credentials_and_invalid_token_rejected(self):
        with self.assertRaises(ValueError): Client(self.data/'cache','http://user:secret@localhost:7890')
        with self.assertRaises(ValueError): Client(self.data/'cache','http://localhost:7890','PRIVATE\nVALUE')

    def test_authorization_cross_site_redirect_refused(self):
        req=urllib.request.Request('https://api.bgm.tv/v0/subjects/10',headers={'Authorization':'Bearer PRIVATE'})
        with self.assertRaises(ValueError): SafeRedirect().redirect_request(req,None,302,'',{},'https://lain.bgm.tv/x')

    def test_alive_process_lock_cannot_unlock(self):
        atomic_json(self.data/'execution.lock',{'pid':os.getpid(),'run':'current'})
        with self.assertRaises(ValueError): unlock_stale(self.data)

    def test_jellyfin_restored_after_body_failure(self):
        if os.name!='nt': self.skipTest('Windows service helper')
        with mock.patch('bgmtool.executor.ctypes.windll.shell32.IsUserAnAdmin',return_value=True), mock.patch('bgmtool.executor.service_command',side_effect=['Running','','']) as command:
            with self.assertRaises(RuntimeError):
                with pause_jellyfin(True,lambda s:None): raise RuntimeError('simulated failure')
            self.assertIn('Start-Service',command.call_args[0][0])

    def test_jellyfin_restored_after_stop_failure(self):
        if os.name!='nt': self.skipTest('Windows service helper')
        with mock.patch('bgmtool.executor.ctypes.windll.shell32.IsUserAnAdmin',return_value=True), mock.patch('bgmtool.executor.service_command',side_effect=['Running',RuntimeError('stop timeout'),'']) as command:
            with self.assertRaises(RuntimeError):
                with pause_jellyfin(True,lambda s:None): pass
            self.assertIn('Start-Service',command.call_args[0][0])

    def test_originally_stopped_jellyfin_stays_stopped(self):
        if os.name!='nt': self.skipTest('Windows service helper')
        with mock.patch('bgmtool.executor.ctypes.windll.shell32.IsUserAnAdmin',return_value=True), mock.patch('bgmtool.executor.service_command',return_value='Stopped') as command:
            with pause_jellyfin(True,lambda s:None): pass
            self.assertEqual(command.call_count,1)

    def test_parser_and_reserved_names(self):
        for name,expected in [('Anime S02E03-E04.mkv',(3,4,2)),('[Team] Anime [12v2][1080p].mkv',(12,None,None)),
                              ('作品 第3集.mp4',(3,None,None)),('Anime [1920x1080].mkv',(None,None,None))]:
            self.assertEqual(episode_number(name),expected)
        self.assertEqual(safe('CON'),'_CON'); self.assertNotIn(':',safe('A:B'))

    def test_search_suggest_exact_above_uncertain(self):
        rows=suggest('Test Show',[{'id':2,'name':'Other Show'},{'id':1,'name':'Test Show'}])
        self.assertEqual(rows[0]['id'],1); self.assertEqual(rows[0]['match_score'],1)

    def test_decimal_override_refused_not_truncated(self):
        result=scan(self.config); g=result['groups'][0]
        choices={g['id']:{'subject_id':10,'confirmed':True,'overrides':{self.files[0].name:{'number':1.5}}}}
        with self.assertRaises(ValueError): Planner(self.config,self.data,FakeClient(),lambda s:None).build([g],choices)
        self.assert_original()

    def test_junction_flag_refused_without_symlink_privilege(self):
        roots=roots_from(self.config)
        with mock.patch('bgmtool.common.reparse',side_effect=lambda p:Path(p)==self.source):
            with self.assertRaises(ValueError): guard(self.files[0],roots)

    def test_new_different_movie_in_managed_folder_not_auto_bound(self):
        folder=self.movies/'Old Film'; folder.mkdir()
        (folder/'movie.nfo').write_text('<movie><bangumiid>20</bangumiid></movie>',encoding='utf-8')
        (folder/'Old Film - 1080p.mkv').write_bytes(b'old')
        (folder/'Another Film.mkv').write_bytes(b'new')
        groups=[g for g in scan(self.config)['groups'] if g['kind']=='movie']
        self.assertEqual(len(groups),1); self.assertIsNone(groups[0]['hint_subject_id'])
        self.assertFalse(groups[0]['managed'])

if __name__=='__main__': unittest.main(verbosity=2)
