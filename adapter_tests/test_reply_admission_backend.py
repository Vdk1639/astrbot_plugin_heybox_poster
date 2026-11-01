from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from adapter_tests.test_repository import make_notification
from xiaoheihe.models import EventState, RoutingTarget
from xiaoheihe.config_service import DEFAULT_CONFIG


async def admit(repo, name, *, uid='1', profile='default', post=None, account='99', **limits):
    notice = replace(make_notification(name, uid, post or name), profile_id=profile)
    event_id = await repo.claim_event(notice)
    return event_id, notice, await repo.reserve_reply_admission(event_id, notice, account_id=account, **limits)


async def test_atomic_floor_user_account_no_partial_charge(repository):
    e, n, first = await admit(repository, 'one', user_daily_limit=1, account_daily_limit=2)
    duplicate = await repository.reserve_reply_admission(e, n, account_id='99')
    assert first['created'] and not duplicate['created'] and duplicate['id']==first['id']
    _, _, floor = await admit(repository, 'two', uid='2', profile='other', post='one')
    assert floor['reason']=='reply_scope_taken'
    _, _, user = await admit(repository, 'three', user_daily_limit=1)
    assert user['reason']=='reply_user_daily_limit'
    _, _, second = await admit(repository, 'four', uid='2', profile='other', account_daily_limit=2)
    assert second['created']
    _, _, total = await admit(repository, 'five', uid='3', account_daily_limit=2)
    assert total['reason']=='reply_account_daily_limit'
    assert (await repository.reply_budget_status('99'))['processing_count']==2


async def test_concurrent_same_attempt_and_last_account_slot(repository):
    n = make_notification('race')
    e = await repository.claim_event(n)
    results = await asyncio.gather(*(repository.reserve_reply_admission(e,n,account_id='99') for _ in range(8)))
    assert sum(r['created'] for r in results)==1
    results = await asyncio.gather(*(admit(repository,str(i),uid=str(i),account_daily_limit=2) for i in range(4)))
    assert sum(r[2].get('created',False) for r in results)==1


async def test_uidless_shared_bucket_owner_only_user_limit_and_disabled_floor(repository):
    await admit(repository,'anon-one',uid='',user_daily_limit=1,thread_once=False)
    _, _, blocked = await admit(repository,'anon-two',uid=' ',user_daily_limit=1,thread_once=False)
    assert blocked['reason']=='reply_user_daily_limit'
    _, _, owner = await admit(repository,'owner',uid='',user_daily_limit=1,owner_exempt=True,account_daily_limit=2)
    assert owner['created']
    _, _, account = await admit(repository,'owner2',uid='',user_daily_limit=1,owner_exempt=True,account_daily_limit=2)
    assert account['reason']=='reply_account_daily_limit'
    _, _, other_uid = await admit(repository,'different-account',uid='',account='98',user_daily_limit=1)
    assert other_uid['created']


async def test_retry_charges_again_and_late_callback_does_not_touch_new_attempt(repository):
    e,n,first = await admit(repository,'retry',account_daily_limit=2)
    assert await repository.schedule_retry(e,'context failed',max_retries=3)==EventState.RETRY_WAIT
    await repository.db.execute('UPDATE incoming_events SET next_retry_at=0 WHERE id=?',(e,))
    assert await repository.claim_retry_event(n)==e
    second = await repository.reserve_reply_admission(e,n,account_id='99',account_daily_limit=2)
    assert second['created'] and second['id']!=first['id']
    assert not await repository.mark_event(e,EventState.IGNORED,admission_id=first['id'])
    assert await repository.schedule_retry(e,'late',max_retries=0,admission_id=first['id'])==EventState.CLAIMED
    await repository.settle_reply_admission(e,'failed',release=True,admission_id=first['id'])
    row = await repository.db.fetchone('SELECT * FROM reply_admissions WHERE id=?',(second['id'],))
    assert row['outcome']=='processing' and row['floor_active']==1
    assert (await repository.reply_budget_status('99'))['processing_count']==2


async def test_failed_retry_budget_does_not_lose_floor_or_bind_new_account(repository):
    e,n,first = await admit(repository,'retry-cap',account_daily_limit=1)
    await repository.schedule_retry(e,'failed',max_retries=3)
    await repository.db.execute('UPDATE incoming_events SET next_retry_at=0 WHERE id=?',(e,))
    await repository.claim_retry_event(n)
    blocked = await repository.reserve_reply_admission(e,n,account_id='99',account_daily_limit=1)
    assert blocked['reason']=='reply_account_daily_limit'
    floor = await repository.db.fetchone('SELECT floor_active FROM reply_admissions WHERE id=?',(first['id'],))
    assert floor[0]==1
    assert (await repository.reserve_reply_admission(e,n,account_id='98'))['reason']=='reply_account_changed'


@pytest.mark.parametrize('status',['pending','sending','sent','send_unknown'])
async def test_legacy_inactive_duplicate_floor_still_blocks_after_first_rejected(repository,status):
    a,_,_ = await admit(repository,'legacy-a',account='88')
    b = await repository.claim_event(make_notification('legacy-b',post='legacy-a'))
    # Restore genuinely old sources, without charging a present-day generation.
    await repository.db.execute('DELETE FROM reply_admissions WHERE incoming_event_id=?',(a,))
    await repository.restore_reply_admission(a,'88','pending')
    await repository.restore_reply_admission(b,'88',status)
    rows = await repository.db.fetchall('SELECT floor_active FROM reply_admissions ORDER BY id')
    assert [r[0] for r in rows]==[1,0]
    await repository.settle_reply_admission(a,'rejected',release=True)
    _,_,blocked = await admit(repository,'new-floor',post='legacy-a',account='88')
    assert blocked['reason']=='reply_scope_taken'
    assert (await repository.reply_budget_status('88'))['processing_count']==0


async def test_send_budget_idempotency_retry_unknown_and_shared_accounts(repository,monkeypatch):
    route = RoutingTarget('default','p','r','r')
    first = await repository.reserve_comment_attempt('default',None,route,'hello',account_id='99',send_key='review:1',daily_limit=1)
    duplicate = await repository.reserve_comment_attempt('default',None,route,'hello',account_id='99',send_key='review:1',daily_limit=1)
    assert first['created'] and not duplicate['created'] and duplicate['id']==first['id']
    other = RoutingTarget('other','q','r','r')
    blocked = await repository.reserve_comment_attempt('other',None,other,'new',account_id='99',send_key='review:2',daily_limit=1)
    assert blocked['reason']=='comment_account_daily_limit'
    await repository.db.execute("UPDATE outgoing_replies SET status='retry_wait' WHERE id=?",(first['id'],))
    retried = await repository.reserve_comment_attempt('default',None,route,'hello',account_id='99',send_key='review:1',daily_limit=1)
    assert retried['created']
    await repository.db.execute("UPDATE outgoing_replies SET status='send_unknown' WHERE id=?",(retried['id'],))
    now = time.time()
    monkeypatch.setattr('xiaoheihe.repository.time.time',lambda:now+86400)
    unknown = await repository.reserve_comment_attempt('default',None,route,'hello',account_id='99',send_key='review:1',daily_limit=1)
    assert not unknown['created'] and unknown['status']=='send_unknown'
    assert (await repository.reply_budget_status('99'))['comment_count']==0
    next_day = await repository.reserve_comment_attempt('other',None,other,'new',account_id='99',send_key='review:2',daily_limit=1)
    assert next_day['created'] and (await repository.reply_budget_status('99'))['comment_count']==1


async def test_concurrent_comment_reservation_one_creator(repository):
    route = RoutingTarget('default','p','r','r')
    results = await asyncio.gather(*(repository.reserve_comment_attempt('default',None,route,'hello',account_id='99',send_key='review:1',daily_limit=1) for _ in range(10)))
    assert sum(r['created'] for r in results)==1
    assert (await repository.reply_budget_status('99'))['comment_count']==1


async def test_cleanup_keeps_current_budget_pending_unknown_and_releases_old_success(repository):
    e,n,first = await admit(repository,'old-success')
    await repository.settle_reply_admission(e,'sent')
    await repository.db.execute("UPDATE reply_admissions SET day='2020-01-01',updated_at=1 WHERE id=?",(first['id'],))
    unknown,_,u = await admit(repository,'old-unknown')
    await repository.settle_reply_admission(unknown,'send_unknown')
    await repository.db.execute("UPDATE reply_admissions SET day='2020-01-01',updated_at=1 WHERE id=?",(u['id'],))
    current,_,c = await admit(repository,'today-rejected')
    await repository.settle_reply_admission(current,'rejected',release=True)
    await repository.db.execute('UPDATE reply_admissions SET updated_at=1 WHERE id=?',(c['id'],))
    await repository.cleanup(DEFAULT_CONFIG['retention'])
    assert await repository.db.fetchone('SELECT 1 FROM reply_admissions WHERE id=?',(first['id'],)) is None
    assert await repository.db.fetchone('SELECT 1 FROM reply_admissions WHERE id=?',(u['id'],)) is not None
    assert await repository.db.fetchone('SELECT 1 FROM reply_admissions WHERE id=?',(c['id'],)) is not None


@pytest.mark.parametrize('outcome,completed,restore',[
    ('sent',1,False),('sent',None,True),('pending',1,True),('send_unknown',1,True),
])
async def test_historical_restore_never_revives_expired_sent_floor(repository,outcome,completed,restore):
    n = make_notification('expired','1','old-post')
    e = await repository.claim_event(n)
    await repository.db.execute("UPDATE incoming_events SET status='sent',completed_at=? WHERE id=?",(completed,e))
    await repository.restore_reply_admission(e,'99',outcome,dedup_days=1)
    await repository.restore_reply_admission(e,'99',outcome,dedup_days=1)
    row = await repository.db.fetchone('SELECT 1 FROM reply_admissions WHERE incoming_event_id=?',(e,))
    assert (row is not None)==restore
    fresh = replace(make_notification('new','2','old-post'),profile_id='other')
    fresh_id = await repository.claim_event(fresh)
    result = await repository.reserve_reply_admission(fresh_id,fresh,account_id='99',dedup_days=1)
    assert result['allowed']==(not restore)


async def test_restore_uses_review_time_only_when_event_completion_missing(repository):
    n = make_notification('review-time','1')
    e = await repository.claim_event(n)
    await repository.restore_reply_admission(e,'99','sent',dedup_days=1,completed_at=1)
    assert await repository.db.fetchone('SELECT 1 FROM reply_admissions') is None
    await repository.db.execute('UPDATE incoming_events SET completed_at=? WHERE id=?',(time.time(),e))
    await repository.restore_reply_admission(e,'99','sent',dedup_days=1,completed_at=1)
    assert await repository.db.fetchone('SELECT 1 FROM reply_admissions') is not None


async def test_pre_request_refresh_crosses_utc_day_and_preserves_unknown_fact(repository,monkeypatch):
    now = 1793491199.0
    monkeypatch.setattr('xiaoheihe.repository.time.time',lambda:now)
    route = RoutingTarget('default','p','r','r')
    old = await repository.reserve_comment_attempt('default',None,route,'old',account_id='99',send_key='old',daily_limit=1)
    now += 86400
    current = await repository.reserve_comment_attempt('default',None,route,'new',account_id='99',send_key='new',daily_limit=1)
    denied = await repository.refresh_comment_attempt(old['id'],account_id='99',daily_limit=1)
    assert not denied['allowed'] and denied['reason']=='comment_account_daily_limit'
    assert denied['attempted_at']==old['attempted_at']
    own = await repository.refresh_comment_attempt(current['id'],account_id='99',daily_limit=1)
    assert own['allowed'] and own['attempted_at']==now
    await repository.db.execute("UPDATE outgoing_replies SET status='retry_wait' WHERE id=?",(current['id'],))
    refreshed = await repository.refresh_comment_attempt(old['id'],account_id='99',daily_limit=1)
    assert refreshed['allowed'] and refreshed['attempted_at']==now
    await repository.db.execute("UPDATE outgoing_replies SET status='send_unknown' WHERE id=?",(old['id'],))
    now += 86400
    with pytest.raises(ValueError):
        await repository.refresh_comment_attempt(old['id'],account_id='99',daily_limit=1)
    assert (await repository.db.fetchone('SELECT attempted_at FROM outgoing_replies WHERE id=?',(old['id'],)))[0]==refreshed['attempted_at']


async def test_verified_upgrade_send_facts_join_current_account_budget(repository):
    n = make_notification('old-outgoing')
    e = await repository.claim_event(n)
    await repository.record_outgoing_attempt('default',e,n.route,'old',status='send_unknown')
    assert (await repository.reply_budget_status('99'))['comment_count']==0
    await repository.restore_reply_admission(e,'99','send_unknown')
    assert (await repository.reply_budget_status('99'))['comment_count']==1
    assert (await repository.reply_budget_status('99'))['processing_count']==0


async def test_late_terminal_callback_cannot_release_pending_source(repository):
    e,_,a = await admit(repository,'pending')
    await repository.settle_reply_admission(e,'pending')
    assert not await repository.mark_event(e,EventState.IGNORED,admission_id=a['id'])
    assert not await repository.mark_event(e,EventState.DRY_RUN,admission_id=a['id'])
    row = await repository.db.fetchone('SELECT outcome,floor_active FROM reply_admissions WHERE id=?',(a['id'],))
    assert tuple(row)==('pending',1)
