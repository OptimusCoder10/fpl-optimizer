"""Synthetic ordinary-free-transfer proof and separate stage/failure evidence."""

from collections import Counter
from dataclasses import replace
from decimal import Decimal
import json
import math
from pathlib import Path
import random
import statistics
import time

import pulp
import pytest

from fpl_optimizer.schemas.transfers import OwnedPlayer, TransferInputs, TransferPlayer, TransferStage
from fpl_optimizer.services import optimization_engine as runtime
from fpl_optimizer.services import transfer_optimization as engine
from fpl_optimizer.services.transfer_validation import validate_transfer_solution
from transfer_oracle import exhaustive_transfers


def inputs():
    positions = ['GK']*2 + ['DEF']*5 + ['MID']*5 + ['FWD']*3
    players = tuple(TransferPlayer(i, p, (i-1)//3+1, 1000, 50, 'test-season', True, True, False, True)
                    for i, p in enumerate(positions, 1))
    extras = (replace(players[0], player_id=16, club_id=6, score_m=2000),
              replace(players[2], player_id=17, club_id=7, score_m=3000))
    return TransferInputs(players+extras, tuple(OwnedPlayer(i, 50) for i in range(1,16)),
                          'test-season', 0, 2, 0)


def change_player(request, player_id, **changes):
    return replace(request, players=tuple(replace(p, **changes) if p.player_id == player_id else p
                                          for p in request.players))


def solve(request):
    result = engine.solve_transfers(request, 5)
    assert result.status == 'optimal', result
    assert result.primary_proven and result.fewer_transfers_proven
    validate_transfer_solution(request, result.solution, scenario=result.selected_scenario)
    assert (result.solution.lineup.score_units, -result.solution.transfer_count) == exhaustive_transfers(request)
    return result


def raw_solve(request, tmp_path, fixed=None):
    prepared, players, _ = engine._prepare(request, 3)
    from dataclasses import asdict
    return engine._solve_stage_worker({'inputs': asdict(prepared),
        'active_players': [asdict(p) for p in players], 'fixed_score': fixed,
        'deadline': time.monotonic()+3, 'scratch': str(tmp_path)})


def test_free_transfer_objective_and_money():
    request = replace(inputs(), bank_tenths=7)
    request = change_player(request, 16, current_price_tenths=52)
    result = solve(request)
    assert result.solution.bought == (16, 17)
    assert result.solution.transfer_count == 2
    assert result.solution.remaining_bank_tenths == 5
    assert result.solution.lineup.score_units == 17000
    assert result.fewer_transfers.status == 'optimal'


def test_hold_wins_equal_primary_scores():
    request = replace(inputs(), players=tuple(replace(p, score_m=1000) for p in inputs().players))
    result = solve(request)
    assert result.selected_scenario == 'hold'
    assert result.solution.transfer_count == 0
    assert result.transfer_primary.solution.lineup.score_units == result.solution.lineup.score_units
    assert result.fewer_transfers.solution.transfer_count == 1


def test_required_fewer_transfers_stage_preserves_primary_score():
    request = change_player(inputs(), 16, score_m=1000)
    result = solve(request)
    assert result.selected_scenario == 'transfers'
    assert result.solution.bought == (17,)
    assert result.solution.lineup.score_units == result.transfer_primary.solution.lineup.score_units
    assert result.fewer_transfers.solution.transfer_count == 1


@pytest.mark.parametrize('free,made', [(0,0), (5,20), (1,19), (5,19), (5,18)])
def test_remaining_allowance_and_already_made_cap(free, made):
    result = solve(replace(inputs(), free_transfers=free, transfers_made=made))
    assert result.solution.transfer_count == min(2, free, 20-made)
    if not free or made == 20:
        assert result.transfer_primary.status == 'infeasible'
        assert result.fewer_transfers.status == 'not_applicable'


def test_inherited_club_excess_hold_is_legal_and_transfers_resolve_it():
    request = change_player(inputs(), 4, club_id=1)
    held = solve(replace(request, free_transfers=0))
    assert held.selected_scenario == 'hold'
    result = solve(request)
    clubs = {p.player_id:p.club_id for p in request.players}
    assert max(Counter(clubs[i] for i in result.solution.squad).values()) <= 3


def test_transfer_must_resolve_excess_even_when_an_unrelated_swap_is_better():
    request = change_player(inputs(), 4, club_id=1)
    request = change_player(request, 16, position='FWD', score_m=10000)
    request = replace(request, free_transfers=1, locked_ids=(1,2,3,4))
    result = solve(request)
    assert result.transfer_primary.status == 'infeasible'
    assert result.selected_scenario == 'hold'


def test_lock_does_not_force_start_and_unowned_lock_prevents_hold():
    request = change_player(inputs(), 16, score_m=-10000)
    request = replace(request, locked_ids=(16,), free_transfers=1)
    result = solve(request)
    assert result.hold_primary.status == 'infeasible'
    assert 16 in result.solution.squad and 16 not in result.solution.lineup.starting_xi


def test_owned_exclusion_forces_sale_without_silently_ignoring_it_in_hold():
    result = solve(replace(inputs(), excluded_ids=(1,), free_transfers=1))
    assert result.solution.sold == (1,)
    assert result.hold_primary.reason == 'hold_conflicts_with_preferences'


def test_unresolved_prices_retain_only_affected_players():
    request = replace(inputs(), owned=tuple(OwnedPlayer(i, None if i in (1,2) else 50) for i in range(1,16)))
    result = solve(request)
    assert {1,2} <= set(result.solution.squad)
    assert result.solution.bought == (17,)
    assert result.retained_unresolved_price_ids == (1,2)


@pytest.mark.parametrize('field,value,reason', [
    ('season_id','old-season','wrong_season'), ('can_select',False,'cannot_select'),
    ('can_transact',False,'cannot_transact'), ('removed',True,'removed'),
    ('availability_resolved',False,'unresolved_availability'), ('score_m',None,'missing_prediction'),
])
def test_purchase_eligibility_is_applied_per_candidate(field,value,reason):
    request = change_player(inputs(), 17, **{field:value})
    _,active,exclusions=engine._prepare(request,5)
    assert 17 not in {p.player_id for p in active}, 'ineligible player entered the model'
    assert exclusions[0].reasons == (reason,)
    result = solve(request)
    assert 17 not in result.solution.squad
    assert result.eligible_candidate_count == 16
    assert (result.excluded_candidates[0].player_id, result.excluded_candidates[0].reasons) == (17,(reason,))


def test_owned_unavailable_or_removed_players_can_remain():
    request = inputs()
    for i in (1,2):
        request = change_player(request,i,can_select=False,can_transact=False,removed=True,availability_resolved=False)
    result = solve(replace(request, locked_ids=(1,2)))
    assert {1,2} <= set(result.solution.squad)


@pytest.mark.parametrize('scores', [[0]*17, [-1000]*17, list(range(-8,9))])
def test_zero_negative_and_tied_transfer_scores(scores):
    request = replace(inputs(), players=tuple(replace(p,score_m=s) for p,s in zip(inputs().players,scores)))
    solve(request)


@pytest.fixture(scope='module')
def timings():
    samples=[]
    yield samples
    if samples:
        ordered=sorted(samples)
        print('\nTRANSFER_RANDOM_TIMINGS '+json.dumps(dict(cases=len(samples), total=sum(samples),
            minimum=min(samples), median=statistics.median(samples),
            p95=ordered[math.ceil(.95*len(samples))-1], maximum=max(samples))))


@pytest.mark.parametrize('seed',range(200))
def test_seeded_transfers_match_exhaustive_objective_tuple(seed,timings):
    rng=random.Random(seed)
    request=inputs()
    # 13 locked owned members: enumerate all legal squads, XIs and captains
    # in the remaining small variable portion, including the hold scenario.
    locks=tuple(i for i in range(1,16) if i not in (2,7))
    players=tuple(replace(p,score_m=rng.choice([-1000,0,1000]) if seed%3==0 else rng.randint(-2000,10000),
                          current_price_tenths=rng.randint(40,65),
                          appearance_probability=rng.choice([None,0,.5,1])) for p in request.players)
    request=replace(request,players=players,owned=tuple(OwnedPlayer(i,30) for i in range(1,16)),
                    bank_tenths=rng.randint(0,80),locked_ids=locks,free_transfers=rng.randint(0,5),
                    transfers_made=rng.choice([0,18,19,20]))
    result=solve(request)
    timings.append(result.elapsed_seconds)
    assert result.bench_bank_stages == 'not_enabled'


@pytest.mark.parametrize('field,value', [('bank_tenths',-1),('bank_tenths',1.0),('bank_tenths',True),
    ('free_transfers',-1),('free_transfers',6),('free_transfers',1.0),('transfers_made',21),('transfers_made',True)])
def test_rejects_invalid_finance_allowance_inputs(field,value):
    with pytest.raises(ValueError):
        engine.solve_transfers(replace(inputs(),**{field:value}),1)


@pytest.mark.parametrize('price',[-1,51,1.0,True])
def test_rejects_invalid_sale_price(price):
    with pytest.raises(ValueError,match='selling price'):
        engine.solve_transfers(replace(inputs(),owned=(OwnedPlayer(1,price),*inputs().owned[1:])),1)


@pytest.mark.parametrize('change,match', [
    ({'owned':inputs().owned[:-1]},'15 owned'),
    ({'owned':(inputs().owned[0],)*15},'duplicate owned'),
    ({'owned':(OwnedPlayer(99,50),*inputs().owned[1:])},'missing'),
    ({'players':inputs().players+(inputs().players[0],)},'duplicate candidate'),
    ({'locked_ids':(1,), 'excluded_ids':(1,)},'lock/exclude conflict'),
    ({'locked_ids':(99,)},'lock/exclusion'),
    ({'excluded_ids':(True,)},'lock/exclusion'),
    ({'excluded_ids':(1,), 'owned':(OwnedPlayer(1,None),*inputs().owned[1:])},'unresolved-price'),
])
def test_rejects_invalid_ownership_and_preferences(change,match):
    with pytest.raises(ValueError,match=match):
        engine.solve_transfers(replace(inputs(),**change),1)


@pytest.mark.parametrize('field,value', [('score_m',1.0),('score_m',True),('score_m',None),
    ('position','BAD'),('position','MID'),('club_id',0),('current_price_tenths',-1),
    ('current_price_tenths',1.5),('can_select',None),('season_id','old-season'),
    ('appearance_probability',float('nan')),('appearance_probability',1.1)])
def test_rejects_invalid_owned_player(field,value):
    with pytest.raises(ValueError):
        engine.solve_transfers(change_player(inputs(),1,**{field:value}),1)


def test_rejects_ineligible_lock():
    with pytest.raises(ValueError,match='locked candidate'):
        engine.solve_transfers(replace(change_player(inputs(),17,can_select=False),locked_ids=(17,)),1)


@pytest.mark.parametrize('budget',[0,-1,True,float('inf'),float('nan'),10**400])
def test_rejects_invalid_budget(budget):
    with pytest.raises(ValueError,match='budget'):
        engine.solve_transfers(inputs(),budget)


def test_constraint_money(tmp_path):
    request=change_player(inputs(),17,current_price_tenths=1000)
    raw=raw_solve(request,tmp_path)
    assert raw['x'][16] == 0, 'unaffordable purchase selected'


def test_constraint_positive_transfers(tmp_path):
    request=change_player(change_player(inputs(),16,score_m=-10000),17,score_m=-10000)
    for i in (2,6,7,15): request=change_player(request,i,score_m=0)
    request=replace(request,locked_ids=(2,6,7))
    raw=raw_solve(request,tmp_path)
    assert raw['transfers'] >= 1


def test_constraint_free_cap(tmp_path):
    raw=raw_solve(replace(inputs(),free_transfers=1),tmp_path)
    assert raw['transfers'] <= 1


def test_constraint_ordinary_cap(tmp_path):
    raw=raw_solve(replace(inputs(),transfers_made=19),tmp_path)
    assert raw['transfers'] <= 1


def test_constraint_club_limit(tmp_path):
    request=change_player(inputs(),17,club_id=1)
    request=change_player(change_player(request,3,score_m=5000),16,score_m=-10000)
    request=replace(request,free_transfers=1)
    raw=raw_solve(request,tmp_path)
    assert sum(raw['x'][i-1] for i in (1,2,3,17)) <= 3


def test_constraint_lock(tmp_path):
    request=replace(inputs(),locked_ids=(1,2))
    raw=raw_solve(request,tmp_path)
    assert raw['x'][0] == raw['x'][1] == 1


def test_constraint_exclusion(tmp_path):
    raw=raw_solve(replace(inputs(),excluded_ids=(17,)),tmp_path)
    assert raw['x'][16] == 0


def test_constraint_unresolved_price(tmp_path):
    request=replace(inputs(),owned=tuple(OwnedPlayer(i,None if i in (1,2) else 50) for i in range(1,16)),bank_tenths=100)
    raw=raw_solve(request,tmp_path)
    assert raw['x'][0] == raw['x'][1] == 1


def test_constraint_fixed_primary_score(tmp_path):
    raw=raw_solve(inputs(),tmp_path,fixed=17000)
    assert raw['score'] == 17000
    assert raw['transfers'] == 2


def test_constraint_xi_count(tmp_path):
    assert sum(raw_solve(inputs(),tmp_path)['y']) == 11


def test_constraint_gk_count(tmp_path):
    request=inputs()
    for i in (1,2,16): request=change_player(request,i,score_m=10000)
    raw=raw_solve(request,tmp_path)
    assert sum(raw['y'][i-1] for i in (1,2,16)) == 1


def test_constraint_def_min(tmp_path):
    request=replace(inputs(),players=tuple(replace(p,score_m=-1000 if p.position=='DEF' else 1000) for p in inputs().players))
    raw=raw_solve(request,tmp_path)
    assert sum(v for p,v in zip(request.players,raw['y']) if p.position=='DEF') >= 3


def test_constraint_fwd_min(tmp_path):
    request=replace(inputs(),players=tuple(replace(p,score_m=-1000 if p.position=='FWD' else 1000) for p in inputs().players))
    assert sum(raw_solve(request,tmp_path)['y'][12:15]) >= 1


def test_constraint_captain_count(tmp_path):
    assert sum(raw_solve(inputs(),tmp_path)['c']) == 1


def test_constraint_starter_owned():
    request=inputs()
    problem,x,y,c,*_=engine._build_transfer_model(request,request.players)
    for i in x: problem += x[i] == int(i in set(range(1,16))-{1}|{16})
    # ID17 is better than the owned defenders but is not selected in this probe.
    problem.solve(pulp.PULP_CBC_CMD(msg=False,timeLimit=2))
    assert y[17].value() == 0, 'unowned starter selected'


def test_constraint_captain_starts():
    request=inputs()
    problem,x,y,c,*_=engine._build_transfer_model(request,request.players)
    problem += y[17] == 0
    problem.solve(pulp.PULP_CBC_CMD(msg=False,timeLimit=2))
    assert c[17].value() == 0, 'benched captain selected'


@pytest.mark.parametrize('name,pos,rhs,sense', [
    ('squad_count',None,15,0),('squad_GK','GK',2,0),('squad_DEF','DEF',5,0),
    ('squad_MID','MID',5,0),('squad_FWD','FWD',3,0),
    ('def_max','DEF',5,-1),('mid_min','MID',2,1),('mid_max','MID',5,-1),('fwd_max','FWD',3,-1),
])
def test_redundant_bounds_have_the_declared_algebra(name,pos,rhs,sense):
    request=inputs()
    problem,x,y,*_=engine._build_transfer_model(request,request.players)
    assert name in problem.constraints, f'missing constraint: {name}'
    row=problem.constraints[name]
    variables=x if name.startswith('squad') else y
    expected={variables[p.player_id]:1 for p in request.players if pos is None or p.position==pos}
    assert dict(row.items()) == expected
    assert (row.constant,row.sense) == (-rhs,sense)


@pytest.mark.parametrize('group',[1,2,3])
def test_binary_variable_domains(group):
    model=engine._build_transfer_model(inputs(),inputs().players)
    assert all(v.isBinary() for v in model[group].values())


def test_epsilon_bench_bonus_would_sacrifice_primary_score():
    request=inputs()
    bench={2,6,7,15}
    request=replace(request,players=tuple(replace(p,score_m=0 if p.player_id in bench else 10000)
                                         for p in request.players),free_transfers=1,
                    owned=tuple(OwnedPlayer(i,0 if i in (6,7) else 50) for i in range(1,16)))
    request=change_player(request,16,position='DEF',score_m=10001,current_price_tenths=50)
    request=change_player(request,17,score_m=900,current_price_tenths=0)
    correct=solve(request)
    assert correct.solution.lineup.score_units == 120002
    problem,x,y,c,score,*_=engine._build_transfer_model(request,request.players)
    problem.setObjective(score + .1*pulp.lpSum(p.score_m*(x[p.player_id]-y[p.player_id]) for p in request.players))
    problem.solve(pulp.PULP_CBC_CMD(msg=False,timeLimit=2))
    assert pulp.value(score) == 120000


@pytest.fixture(scope='module')
def stages():
    request=change_player(inputs(),16,score_m=1000)
    best=engine.solve_transfers(request,5)
    two=engine.solve_transfers(replace(request,locked_ids=(16,17)),5)
    assert best.solution.transfer_count == 1 and two.solution.transfer_count == 2
    assert best.solution.lineup.score_units == two.solution.lineup.score_units
    return request,best.hold_primary,TransferStage('optimal',two.solution),best.fewer_transfers


@pytest.mark.parametrize('status,incumbent', [
    ('timeout_no_solution',False),('feasible_unproven',True),('solver_failure',False),
    ('infeasible',False),('optimal',True),
])
def test_stage_two_outcomes_preserve_primary_proof(monkeypatch,stages,status,incumbent):
    request,hold,primary,secondary=stages
    calls=[]
    monkeypatch.setattr(engine,'_hold_stage',lambda *args:hold)
    def stage(inputs,players,deadline,fixed_score=None):
        calls.append((deadline,fixed_score))
        return primary if fixed_score is None else replace(secondary,status=status,solution=secondary.solution if incumbent else None)
    monkeypatch.setattr(engine,'_transfer_stage',stage)
    result=engine.solve_transfers(request,5)
    assert calls[0][0] == calls[1][0]
    assert calls[1][1] == primary.solution.lineup.score_units
    assert result.status == 'optimal' and result.primary_proven
    assert result.solution == (secondary.solution if incumbent else primary.solution)
    assert result.fewer_transfers_proven == (status=='optimal')
    assert result.fewer_transfers.status == ('solver_failure' if status=='infeasible' else status)


@pytest.mark.parametrize('status,expected',[('feasible_unproven','feasible_unproven'),('optimal','solver_failure')])
def test_worse_secondary_incumbent_keeps_better_saved_solution(monkeypatch,stages,status,expected):
    request,hold,two_transfers,one_transfer=stages
    monkeypatch.setattr(engine,'_hold_stage',lambda *args:hold)
    def stage(i,p,d,fixed=None):
        return one_transfer if fixed is None else replace(two_transfers,status=status)
    monkeypatch.setattr(engine,'_transfer_stage',stage)
    result=engine.solve_transfers(request,5)
    assert result.solution == one_transfer.solution
    assert result.primary_proven and not result.fewer_transfers_proven
    assert result.fewer_transfers.status == expected


@pytest.mark.parametrize('status,incumbent',[('timeout_no_solution',False),('feasible_unproven',True),('solver_failure',False)])
def test_stage_one_without_proof_never_starts_stage_two(monkeypatch,stages,status,incumbent):
    request,hold,primary,_=stages
    monkeypatch.setattr(engine,'_hold_stage',lambda *args:hold)
    calls=[]
    def stage(inputs,players,deadline,fixed_score=None):
        calls.append(fixed_score)
        return replace(primary,status=status,solution=primary.solution if incumbent else None)
    monkeypatch.setattr(engine,'_transfer_stage',stage)
    result=engine.solve_transfers(request,5)
    assert calls == [None]
    assert result.status == 'feasible_unproven' and not result.primary_proven
    assert result.fewer_transfers.status == 'not_run'
    assert result.solution == (primary.solution if incumbent else hold.solution)


def test_unproven_hold_prevents_claiming_comparison_optimal(monkeypatch,stages):
    request,_,primary,secondary=stages
    monkeypatch.setattr(engine,'_hold_stage',lambda *args:TransferStage('timeout_no_solution'))
    monkeypatch.setattr(engine,'_transfer_stage',lambda *args:primary if len(args)==3 else secondary)
    result=engine.solve_transfers(request,5)
    assert result.status == 'feasible_unproven' and not result.primary_proven


def test_shared_deadline_prioritizes_primaries_and_keeps_primary_when_budget_expires(monkeypatch,stages):
    from types import SimpleNamespace
    request,hold,primary,_=stages
    now=[100.0]
    calls=[]
    monkeypatch.setattr(engine,'time',SimpleNamespace(monotonic=lambda:now[0]))
    def hold_stage(inputs,players,deadline):
        calls.append(('hold',deadline))
        now[0]=102
        return hold
    def free_stage(inputs,players,deadline,fixed_score=None):
        calls.append(('transfers',deadline))
        assert fixed_score is None
        now[0]=105
        return primary
    monkeypatch.setattr(engine,'_hold_stage',hold_stage)
    monkeypatch.setattr(engine,'_transfer_stage',free_stage)
    result=engine.solve_transfers(request,5)
    assert calls == [('hold',105),('transfers',105)]
    assert result.solution == primary.solution
    assert result.primary_proven and not result.fewer_transfers_proven
    assert (result.fewer_transfers.status,result.fewer_transfers.reason)==('not_run','budget_exhausted')


def test_hold_worker_receives_original_absolute_deadline(monkeypatch):
    captured=[]
    def run(players,deadline):
        captured.append(deadline)
        return {'problem_status':0,'solution_status':0,'timed_out':True}
    monkeypatch.setattr(runtime,'_run_worker',run)
    request,players,_=engine._prepare(inputs(),2)
    deadline=time.monotonic()+2
    result=engine._hold_stage(request,players,deadline)
    assert captured == [deadline]
    assert result.status == 'timeout_no_solution'


@pytest.mark.parametrize('ps,ss,timed_out,expected',[
    (1,1,False,'optimal'),(1,2,True,'feasible_unproven'),(-1,-1,True,'timeout_no_solution'),
    (-1,-1,False,'infeasible'),(0,0,True,'timeout_no_solution'),(-3,0,False,'solver_failure'),
    (-1,2,True,'feasible_unproven'),
])
def test_transfer_stage_statuses_keep_codes(monkeypatch,tmp_path,ps,ss,timed_out,expected):
    request=inputs()
    raw=raw_solve(request,tmp_path)
    raw.update(problem_status=ps,solution_status=ss,timed_out=timed_out)
    monkeypatch.setattr(runtime,'_run_problem',lambda *args:raw)
    stage=engine._transfer_stage(request,request.players,time.monotonic()+2)
    assert stage.status == expected
    assert (stage.problem_status,stage.solution_status)==(ps,ss)
    assert (stage.solution is not None)==(expected in ('optimal','feasible_unproven'))


@pytest.mark.parametrize('key', ['x','y','c','score','transfers','bank','objective'])
def test_invalid_claimed_solver_solution_is_discarded(monkeypatch,tmp_path,key):
    request=inputs()
    raw=raw_solve(request,tmp_path)
    if key in ('x','y','c'): raw[key][0]=.5
    else: raw[key]+=1
    monkeypatch.setattr(runtime,'_run_problem',lambda *args:raw)
    stage=engine._transfer_stage(request,request.players,time.monotonic()+2)
    assert (stage.status,stage.reason,stage.solution)==('solver_failure','invalid_solution',None)


def test_invalid_secondary_output_preserves_saved_primary(monkeypatch,stages,tmp_path):
    request,hold,primary,_=stages
    raw=raw_solve(request,tmp_path,primary.solution.lineup.score_units)
    raw['score']-=1
    monkeypatch.setattr(engine,'_hold_stage',lambda *args:hold)
    real=engine._transfer_stage
    monkeypatch.setattr(engine,'_transfer_stage',lambda i,p,d,fixed=None:primary if fixed is None else real(i,p,d,fixed))
    monkeypatch.setattr(runtime,'_run_problem',lambda *args:raw)
    result=engine.solve_transfers(request,5)
    assert result.solution == primary.solution and result.primary_proven
    assert result.fewer_transfers.reason == 'invalid_solution'


def test_secondary_cannot_escape_fixed_primary_score(monkeypatch,stages,tmp_path):
    request,_,primary,_=stages
    # A valid lower-scoring lineup from a different synthetic input is not a
    # valid output of the fixed-S problem, even if money/ownership are legal.
    raw=raw_solve(request,tmp_path,primary.solution.lineup.score_units)
    monkeypatch.setattr(runtime,'_run_problem',lambda *args:raw)
    stage=engine._transfer_stage(request,request.players,time.monotonic()+2,primary.solution.lineup.score_units+1)
    assert (stage.status,stage.reason)==('solver_failure','invalid_solution')


@pytest.mark.parametrize('field,value', [('remaining_bank_tenths',999),('transfer_count',0),
    ('bought',()),('sold',()),('squad',(1,)*15)])
def test_independent_validator_rejects_corrupt_transfer_output(stages,field,value):
    request,_,_,secondary=stages
    with pytest.raises(ValueError):
        validate_transfer_solution(request,replace(secondary.solution,**{field:value}),scenario='transfers')


@pytest.mark.parametrize('change,reason',[
    ({'free_transfers':0},'remaining free'),({'transfers_made':20},'transfer cap'),
    ({'bank_tenths':-1},'remaining bank'),({'locked_ids':(2,7)},'lock or exclusion'),
    ({'excluded_ids':(17,)},'lock or exclusion'),
])
def test_independent_validator_rechecks_request_constraints(stages,change,reason):
    request,_,_,secondary=stages
    if 'locked_ids' in change:
        change={'locked_ids':secondary.solution.sold}
    with pytest.raises(ValueError,match=reason):
        validate_transfer_solution(replace(request,**change),secondary.solution,scenario='transfers')


def test_independent_validator_rechecks_club_eligibility_prices_and_fixed_score(stages):
    request,_,_,secondary=stages
    solution=secondary.solution
    club_bad=replace(request,players=tuple(replace(p,club_id=1) for p in request.players))
    bad_eligibility=change_player(request,17,can_select=False)
    unknown_sale=replace(request,owned=tuple(replace(o,selling_price_tenths=None) if o.player_id in solution.sold else o for o in request.owned))
    for bad,reason in [(club_bad,'club limit'),(bad_eligibility,'ineligible purchase'),(unknown_sale,'unresolved selling')]:
        with pytest.raises(ValueError,match=reason):
            validate_transfer_solution(bad,solution,scenario='transfers')
    with pytest.raises(ValueError,match='primary score'):
        validate_transfer_solution(request,solution,scenario='transfers',fixed_score=solution.lineup.score_units+1)


def test_transfer_validator_and_oracle_do_not_import_model():
    import ast,inspect
    from fpl_optimizer.services import transfer_validation
    import transfer_oracle
    for module in (transfer_validation,transfer_oracle):
        imports=[n for n in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(n,(ast.Import,ast.ImportFrom))]
        assert all('transfer_optimization' not in ast.unparse(n) and 'optimization_engine' not in ast.unparse(n)
                   and 'pulp' not in ast.unparse(n) for n in imports)


def test_repeatable_tied_transfer_solutions():
    request=change_player(inputs(),16,score_m=1000)
    first=solve(request)
    for seed in range(3):
        shuffled=list(request.players)
        random.Random(seed).shuffle(shuffled)
        assert solve(replace(request,players=tuple(shuffled))).solution == first.solution


def test_assembled_money_rhs_is_checked_for_export_precision():
    result=engine.solve_transfers(replace(inputs(),bank_tenths=10**13+1),3)
    assert result.transfer_primary.reason == 'unsafe_numeric_range'
    assert result.status == 'feasible_unproven'  # hold exists; comparison is not proven


def test_fixed_score_rhs_is_checked_before_cbc(tmp_path):
    raw=raw_solve(inputs(),tmp_path,fixed=10**13+1)
    assert raw == {'failure':'unsafe_numeric_range'}


def test_all_infeasible_constraints_do_not_return_empty_success():
    result=engine.solve_transfers(replace(inputs(),locked_ids=(16,),free_transfers=0),3)
    assert (result.status,result.solution)==('infeasible',None)


def test_exhausted_budget_with_no_incumbent_returns_timeout():
    result=engine.solve_transfers(inputs(),1e-12)
    assert (result.status,result.solution)==('timeout_no_solution',None)


def test_public_engine_transfer_entry_point():
    result=runtime.solve_transfers(replace(inputs(),free_transfers=0),3)
    assert result.status == 'optimal' and result.selected_scenario == 'hold'


def test_unexportable_secondary_rhs_retains_real_primary_result():
    request=replace(inputs(),players=tuple(replace(p,score_m=10**12+(p.player_id==17)) for p in inputs().players))
    result=engine.solve_transfers(request,5)
    assert result.status == 'optimal' and result.primary_proven
    assert not result.fewer_transfers_proven
    assert result.fewer_transfers.reason == 'unsafe_numeric_range'
    assert result.solution == result.transfer_primary.solution
    assert result.solution.lineup.score_units == 12*10**12+2


def test_real_stage_two_worker_timeout_keeps_proven_primary(monkeypatch,stages,tmp_path):
    import subprocess,sys
    request,hold,primary,_=stages
    real_stage=engine._transfer_stage
    monkeypatch.setattr(engine,'_hold_stage',lambda *args:hold)
    monkeypatch.setattr(engine,'_transfer_stage',lambda i,p,d,fixed=None:primary if fixed is None else real_stage(i,p,d,fixed))
    original=subprocess.Popen
    processes=[]
    pid_file=tmp_path/'child.pid'
    def launch(*args,**kwargs):
        code=("import subprocess,sys,time; c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              "open(sys.argv[1],'w').write(str(c.pid)); time.sleep(60)")
        process=original([sys.executable,'-c',code,str(pid_file)],**kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(runtime.subprocess,'Popen',launch)
    result=engine.solve_transfers(request,.5)
    assert result.status == 'optimal' and result.primary_proven
    assert result.solution == primary.solution
    assert result.fewer_transfers.status == 'timeout_no_solution'
    assert not result.fewer_transfers_proven
    assert processes[0].returncode is not None and result.elapsed_seconds < 2
    state=Path(f'/proc/{pid_file.read_text()}/stat')
    for _ in range(100):
        try: stopped=state.read_text().split()[2]=='Z'
        except (FileNotFoundError,ProcessLookupError): stopped=True
        if stopped: break
        time.sleep(.005)
    assert stopped, 'secondary solver child is still running'


def test_unsafe_original_numbers_fail_before_pulp():
    result=engine.solve_transfers(change_player(inputs(),1,score_m=10**30),3)
    assert result.status == 'solver_failure' and result.solution is None
    assert result.transfer_primary.reason == result.hold_primary.reason == 'unsafe_numeric_range'


def test_unsafe_unowned_candidate_does_not_erase_valid_hold():
    result=engine.solve_transfers(change_player(inputs(),17,score_m=10**30),3)
    assert result.status == 'feasible_unproven' and result.selected_scenario == 'hold'
    assert result.hold_primary.status == 'optimal'
    assert result.transfer_primary.reason == 'unsafe_numeric_range'
