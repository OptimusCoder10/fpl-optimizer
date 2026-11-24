"""Exact-hit proofs, offer boundaries and independently checked diagnostics."""
from dataclasses import asdict, replace
from itertools import product
import json
import math
from pathlib import Path
import random
import statistics
import time
from types import SimpleNamespace

import pulp
import pytest

from fpl_optimizer.schemas.transfers import OwnedPlayer, TransferStage
from fpl_optimizer.services import optimization_engine as runtime
from fpl_optimizer.services import transfer_optimization as engine
from fpl_optimizer.services.transfer_validation import validate_hit_comparison, validate_transfer_solution
from test_transfer_optimization import inputs, change_player
from transfer_oracle import exhaustive_transfers


def run(request):
    return runtime.solve_transfers(request, 8, include_hit=True)


def raw_hit(request, tmp_path):
    prepared, players, _ = engine._prepare(request, 3)
    return engine._solve_stage_worker(dict(inputs=asdict(prepared), active_players=[asdict(p) for p in players],
        fixed_score=None, scenario='hit', deadline=time.monotonic()+3, scratch=str(tmp_path)))


def boundary_request(gain):
    # The locked 10-point captain makes the replacement defender contribute once.
    request=replace(inputs(),free_transfers=0,locked_ids=tuple(range(1,7))+tuple(range(8,16)))
    request=change_player(request,3,score_m=10000)
    return change_player(request,17,score_m=1000+gain)


@pytest.mark.parametrize('gain,offered',[(3999,False),(4000,False),(4001,True)])
def test_offer_boundary_and_gross_net(gain,offered):
    result=run(boundary_request(gain))
    hit=result.hit
    assert hit.primary.status == 'optimal'
    assert hit.gross_score_units == result.solution.lineup.score_units+gain
    assert hit.net_score_units == hit.gross_score_units-4000
    assert hit.net_gain_units == gain-4000
    assert hit.gross_score_points == hit.gross_score_units/1000
    assert hit.net_score_points == hit.net_score_units/1000
    assert hit.net_gain_points == (gain-4000)/1000
    assert hit.offered is offered and not hit.advisory
    assert not hit.only_feasible_option
    assert hit.reason == ('proven_improvement' if offered else 'insufficient_net_gain')


def test_already_incurred_hits_are_separate_not_charged_again():
    result=run(replace(boundary_request(4001),already_incurred_hit_units=12000))
    assert result.hit.already_incurred_hit_units == 12000
    assert result.hit.additional_hit_units == 4000
    assert result.hit.net_gain_units == 1 and result.hit.offered


@pytest.mark.parametrize('value',[-4000,1,4000.0,True,None])
def test_invalid_incurred_hit_input(value):
    with pytest.raises(ValueError,match='incurred'):
        run(replace(inputs(),already_incurred_hit_units=value))


@pytest.mark.parametrize('value',[1,None,'yes'])
def test_invalid_hit_switch(value):
    with pytest.raises(ValueError,match='include_hit'):
        engine.solve_transfers(inputs(),1,include_hit=value)


def six_swap_request():
    request=inputs()
    extra=tuple(replace(p,player_id=16+i,club_id=6+i,score_m=5000) for i,p in enumerate(request.players[:6]))
    return replace(request,players=request.players[:15]+extra,free_transfers=5,
        excluded_ids=tuple(range(1,7)),locked_ids=tuple(range(7,16)))


@pytest.mark.parametrize('free,made,allowed',[(0,19,True),(0,20,False),(5,14,True),(5,15,False)])
def test_f_zero_f_five_and_cap_boundary(free,made,allowed):
    request=replace(inputs() if free==0 else six_swap_request(),free_transfers=free,transfers_made=made)
    result=run(request)
    hit=result.hit
    if allowed:
        assert hit.primary.status == 'optimal'
        assert hit.primary.solution.transfer_count == free+1
        assert hit.gross_score_units == exhaustive_transfers(request,scenario='hit')[0]
    else:
        assert hit.primary.status == 'infeasible' and hit.primary.reason == 'ordinary_cap_exceeded'
        assert hit.primary.solution is None and not hit.offered


@pytest.mark.parametrize('unproven',[False,True])
def test_missing_baseline_only_feasible_option(monkeypatch,unproven):
    request=replace(inputs(),free_transfers=0,locked_ids=(16,))
    if unproven:
        real=engine._transfer_stage
        def stage(*args,**kwargs):
            return replace(real(*args,**kwargs),status='feasible_unproven',problem_status=0,solution_status=2)
        monkeypatch.setattr(engine,'_transfer_stage',stage)
    result=run(request)
    assert result.hold_primary.status == result.transfer_primary.status == 'infeasible'
    hit=result.hit
    assert hit.primary.status == ('feasible_unproven' if unproven else 'optimal')
    assert hit.primary.solution is not None and hit.only_feasible_option
    assert not hit.offered and not hit.advisory
    assert hit.reason == 'no_feasible_baseline' and hit.net_gain_units is None
    assert hit.net_score_units == hit.gross_score_units-4000
    assert result.status == 'infeasible' and result.solution is None  # describes hold/free only


@pytest.fixture(scope='module')
def evidence():
    request=boundary_request(4001)
    result=run(request)
    return request,result.hold_primary,result.hit.primary


@pytest.mark.parametrize('hold_status,hit_status',list(product(
    ['optimal','feasible_unproven','timeout_no_solution','infeasible','solver_failure'],
    ['optimal','feasible_unproven','timeout_no_solution','infeasible','solver_failure'])))
def test_proof_combinations(monkeypatch,evidence,hold_status,hit_status):
    request,hold,hit=evidence
    feasible={'optimal','feasible_unproven'}
    monkeypatch.setattr(engine,'_hold_stage',lambda *a:replace(hold,status=hold_status,
        solution=hold.solution if hold_status in feasible else None))
    monkeypatch.setattr(engine,'_transfer_stage',lambda *a,**k:replace(hit,status=hit_status,
        solution=hit.solution if hit_status in feasible else None))
    result=run(request)
    out=result.hit
    assert out.offered == (hold_status=='optimal' and hit_status in feasible)
    assert out.advisory == (hold_status=='optimal' and hit_status=='feasible_unproven')
    assert out.only_feasible_option == (hold_status=='infeasible' and hit_status in feasible)
    assert (out.gross_score_units is not None) == (hit_status in feasible)
    assert (out.net_gain_units is not None) == (hold_status in feasible and hit_status in feasible)


@pytest.mark.parametrize('free_status',['feasible_unproven','timeout_no_solution','solver_failure'])
def test_unproven_free_prevents_offer_even_with_proven_hold(monkeypatch,free_status):
    request=replace(inputs(),free_transfers=1)
    request=change_player(change_player(request,16,score_m=7000),17,score_m=8000)
    actual=run(request)
    assert actual.hit.offered
    monkeypatch.setattr(engine,'_hold_stage',lambda *a:actual.hold_primary)
    def stage(*a,scenario='transfers',**kw):
        if scenario=='hit':return actual.hit.primary
        return replace(actual.transfer_primary,status=free_status,
            solution=actual.transfer_primary.solution if free_status=='feasible_unproven' else None)
    monkeypatch.setattr(engine,'_transfer_stage',stage)
    result=run(request)
    assert result.hit.primary.solution is not None
    assert result.hit.reason == 'baseline_not_proven'
    assert not result.hit.offered and not result.hit.only_feasible_option


def test_three_primaries_before_secondary_share_deadline(monkeypatch):
    request=replace(inputs(),free_transfers=1)
    real_hold,real_stage=engine._hold_stage,engine._transfer_stage
    calls=[]
    def hold(*args):
        calls.append(('hold',args[2]));return real_hold(*args)
    def stage(*args,**kwargs):
        label='fewer' if len(args)==4 else kwargs.get('scenario','transfers')
        calls.append((label,args[2]));return real_stage(*args,**kwargs)
    monkeypatch.setattr(engine,'_hold_stage',hold)
    monkeypatch.setattr(engine,'_transfer_stage',stage)
    result=run(request)
    assert [c[0] for c in calls] == ['hold','transfers','hit','fewer']
    assert len({c[1] for c in calls}) == 1
    assert result.hit.primary.solution.transfer_count == 2
    assert result.fewer_transfers_proven


def test_hit_exhausts_budget_keeps_proven_free_solution(monkeypatch):
    request=replace(inputs(),free_transfers=1)
    proven=run(request)
    now=[100.0]
    monkeypatch.setattr(engine,'time',SimpleNamespace(monotonic=lambda:now[0]))
    monkeypatch.setattr(engine,'_hold_stage',lambda *a:proven.hold_primary)
    def stage(*a,scenario='transfers',**kw):
        assert len(a)==3
        if scenario=='hit':
            now[0]=108.0
            return TransferStage('timeout_no_solution')
        return proven.transfer_primary
    monkeypatch.setattr(engine,'_transfer_stage',stage)
    result=run(request)
    assert result.solution == proven.transfer_primary.solution and result.primary_proven
    assert result.fewer_transfers.status == 'not_run' and result.fewer_transfers.reason == 'budget_exhausted'
    assert result.hit.primary.status == 'timeout_no_solution' and not result.hit.offered


def test_infeasible_hit_preferences():
    result=run(replace(inputs(),free_transfers=0,locked_ids=tuple(range(1,16))))
    assert result.hit.primary.status == 'infeasible' and result.hit.primary.solution is None
    assert not result.hit.only_feasible_option


def test_constraint_exact_hit_count(tmp_path):
    raw=raw_hit(replace(inputs(),free_transfers=0),tmp_path)
    assert raw['transfers'] == 1, 'hit count must equal F+1'


def test_constraint_hit_cap(tmp_path):
    raw=raw_hit(replace(inputs(),free_transfers=1,transfers_made=19),tmp_path)
    assert raw['problem_status'] == -1, 'hit must respect T+n <= 20'


def test_constraint_hit_club(tmp_path):
    request=change_player(inputs(),4,club_id=1)
    request=replace(request,free_transfers=0,locked_ids=(1,2,3,4))
    raw=raw_hit(request,tmp_path)
    assert raw['problem_status'] == -1, 'hit must resolve inherited club excess'
    result=run(request)
    assert result.hold_primary.status == 'optimal' and result.hit.primary.status == 'infeasible'


def test_constraint_hit_money(tmp_path):
    request=change_player(change_player(inputs(),16,current_price_tenths=1000),17,current_price_tenths=1000)
    raw=raw_hit(replace(request,free_transfers=0),tmp_path)
    assert raw['problem_status'] == -1, 'hit must pay confirmed purchase costs'


def test_hit_numeric_export_guard():
    result=run(replace(inputs(),free_transfers=1,bank_tenths=10**13+1))
    assert result.hit.primary.status == 'solver_failure'
    assert result.hit.primary.reason == 'unsafe_numeric_range'


@pytest.mark.parametrize('key',['x','y','c','score','transfers','bank','objective'])
def test_hit_invalid_solver_output_discarded(monkeypatch,tmp_path,key):
    request=replace(inputs(),free_transfers=1)
    raw=raw_hit(request,tmp_path)
    if key in ('x','y','c'):raw[key][0]=.5
    else:raw[key]+=1
    monkeypatch.setattr(runtime,'_run_problem',lambda *a:raw)
    stage=engine._transfer_stage(request,request.players,time.monotonic()+2,scenario='hit')
    assert stage.status == 'solver_failure' and stage.solution is None and stage.reason == 'invalid_solution'


@pytest.mark.parametrize('ps,ss,timed,expected',[(1,1,False,'optimal'),(1,2,True,'feasible_unproven'),
    (-1,-1,True,'timeout_no_solution'),(-1,-1,False,'infeasible'),(-3,0,False,'solver_failure'),
    (0,0,True,'timeout_no_solution'),(-1,2,True,'feasible_unproven')])
def test_hit_status_codes(monkeypatch,tmp_path,ps,ss,timed,expected):
    request=replace(inputs(),free_transfers=1)
    raw=raw_hit(request,tmp_path)
    raw.update(problem_status=ps,solution_status=ss,timed_out=timed)
    monkeypatch.setattr(runtime,'_run_problem',lambda *a:raw)
    stage=engine._transfer_stage(request,request.players,time.monotonic()+2,scenario='hit')
    assert stage.status == expected and (stage.problem_status,stage.solution_status)==(ps,ss)


@pytest.mark.parametrize('field,value',[('gross_score_units',0),('net_score_units',0),('net_gain_units',0),
    ('additional_hit_units',0),('already_incurred_hit_units',4000),('offered',False),
    ('advisory',True),('only_feasible_option',True)])
def test_independent_hit_arithmetic_and_flags(evidence,field,value):
    request,hold,hit=evidence
    comparison=engine._hit_comparison(request,hit,hold,TransferStage('infeasible'),hold.solution,True)
    with pytest.raises(ValueError):
        validate_hit_comparison(request,replace(comparison,**{field:value}),hold,TransferStage('infeasible'),hold.solution,True)


def test_independent_hit_count(evidence):
    request,hold,hit=evidence
    with pytest.raises(ValueError,match=r'F\+1'):
        validate_transfer_solution(replace(request,free_transfers=1),hit.solution,scenario='hit')


def test_hit_rejects_secondary_stage():
    with pytest.raises(ValueError,match='secondary'):
        engine._build_transfer_model(inputs(),inputs().players,12000,scenario='hit')


def test_default_preserves_free_only_api():
    result=runtime.solve_transfers(inputs(),5)
    assert result.hit is None and result.model_version == 'ordinary-free-transfers-v1'


@pytest.mark.parametrize('scores',[[0]*17,[-1000]*17,list(range(-8,9))])
def test_hit_ties_zeros_negative_scores(scores):
    request=replace(inputs(),players=tuple(replace(p,score_m=s) for p,s in zip(inputs().players,scores)),free_transfers=1)
    result=run(request)
    assert result.hit.gross_score_units == exhaustive_transfers(request,scenario='hit')[0]


def test_hit_determinism():
    request=replace(inputs(),free_transfers=1)
    first=run(request)
    for seed in range(3):
        players=list(request.players);random.Random(seed).shuffle(players)
        current=run(replace(request,players=tuple(players)))
        assert current.solution == first.solution
        assert current.hit.primary.solution == first.hit.primary.solution
        assert current.hit.offered == first.hit.offered


@pytest.fixture(scope='module')
def hit_timings():
    samples=[]
    yield samples
    if samples:
        ordered=sorted(samples)
        data=dict(cases=len(samples),total=sum(samples),minimum=min(samples),median=statistics.median(samples),
                  p95=ordered[math.ceil(.95*len(samples))-1],maximum=max(samples))
        Path('/tmp/slice33-hit-timings.json').write_text(json.dumps(data))
        print('\nHIT_RANDOM_TIMINGS '+json.dumps(data))


@pytest.mark.parametrize('seed',range(200))
def test_seeded_hold_free_hit_match_exhaustive(seed,hit_timings):
    rng=random.Random(seed+3300)
    request=inputs()
    players=tuple(replace(p,score_m=rng.choice([-1000,0,1000]) if seed%3==0 else rng.randint(-2000,10000),
        current_price_tenths=rng.randint(40,65),appearance_probability=rng.choice([None,0,.5,1])) for p in request.players)
    request=replace(request,players=players,owned=tuple(OwnedPlayer(i,30) for i in range(1,16)),
        bank_tenths=rng.randint(0,80),locked_ids=tuple(i for i in range(1,16) if i not in (2,7)),
        free_transfers=[0,1,0,1,2,5][seed%6],transfers_made=rng.choice([0,0,18,19,20]))
    result=run(request);hit_timings.append(result.elapsed_seconds)
    ordinary=exhaustive_transfers(request)
    assert (result.solution.lineup.score_units,-result.solution.transfer_count) == ordinary
    assert result.primary_proven and result.fewer_transfers_proven
    for scenario,stage in [('hold',result.hold_primary),('free',result.transfer_primary),('hit',result.hit.primary)]:
        expected=exhaustive_transfers(request,scenario=scenario)
        if expected is None:
            assert stage.status == 'infeasible' and stage.solution is None
        else:
            assert stage.status == 'optimal'
            assert stage.solution.lineup.score_units == expected[0]
    expected=exhaustive_transfers(request,scenario='hit')
    assert result.hit.offered == (expected is not None and expected[0]-ordinary[0]>4000)
    assert result.bench_bank_stages == 'not_enabled'


def test_exact_hit_equality_algebra_and_redundant_lower_bound():
    for f in range(6):
        request=replace(inputs(),free_transfers=f)
        model,x,*_=engine._build_transfer_model(request,request.players,scenario='hit')
        assert 'exact_hit_count' in model.constraints
        row=model.constraints['exact_hit_count']
        assert dict(row.items()) == {x[16]:1,x[17]:1}
        assert row.constant == -(f+1) and row.sense == pulp.LpConstraintEQ
        # F >= 0 and n=F+1 imply n>=1; the free-only upper bound must not apply.
        assert f+1 >= 1
        assert 'positive_transfers' not in model.constraints and 'free_cap' not in model.constraints


def test_invalid_hit_comparison_is_discarded(monkeypatch):
    real=engine._hit_comparison
    def corrupt(*args):
        result=real(*args)
        if result.primary.solution is not None:
            return replace(result,net_score_units=result.net_score_units+1)
        return result
    monkeypatch.setattr(engine,'_hit_comparison',corrupt)
    result=run(boundary_request(4001))
    assert result.hit.primary.status == 'solver_failure'
    assert result.hit.primary.reason == 'invalid_solution'
    assert result.hit.primary.solution is None and not result.hit.offered
    assert result.primary_proven


@pytest.mark.parametrize('status',['timeout_no_solution','solver_failure','feasible_unproven'])
def test_secondary_failure_retains_hit_and_free_primary(monkeypatch,status):
    request=replace(inputs(),free_transfers=1)
    actual=run(request)
    monkeypatch.setattr(engine,'_hold_stage',lambda *a:actual.hold_primary)
    def stage(*a,scenario='transfers',**kw):
        if scenario=='hit':return actual.hit.primary
        if len(a)==3:return actual.transfer_primary
        return TransferStage(status,actual.transfer_primary.solution if status=='feasible_unproven' else None)
    monkeypatch.setattr(engine,'_transfer_stage',stage)
    result=run(request)
    assert result.solution == actual.transfer_primary.solution and result.primary_proven
    assert not result.fewer_transfers_proven
    assert result.hit.primary == actual.hit.primary


def test_hit_unresolved_price_retention_and_purchase_eligibility():
    request=replace(inputs(),free_transfers=0,owned=(OwnedPlayer(1,None),OwnedPlayer(2,None),*inputs().owned[2:]))
    request=change_player(request,17,can_transact=False)
    result=run(request)
    assert result.hit.primary.status == 'infeasible'
    assert result.retained_unresolved_price_ids == (1,2)
    assert result.excluded_candidates[0].player_id == 17
