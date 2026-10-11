from rimagent.registry import tool

@tool('day0_setup','Audit immediate day-0 survival state and return a compact action checklist.', {})
def day0_setup(ctx):
    s=ctx.bridge.call('state.summary')
    food=s.get('food_days',0)
    return {'food_days':food,'checklist':['secure survival food and stockpile' if food<2 else 'food stable','finish smallest roofed shelter','verify campfire/stove bill','plant rice','set modest hunting target 80-120','delay expansion/missions until food>=6 and shelter/defense ready']}
