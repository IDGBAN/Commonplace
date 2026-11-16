"""A made-up journal with planted facts, so an answer can be checked.

Nobody's life is in here. Each fact a scenario turns on is written into an
entry on purpose: who wrote the fix, when the coffee was drunk, how far each
run was. Change an entry and the scenarios that lean on it change with it, so
read scenarios.py before editing.
"""

from datetime import date

# The day the questions are asked on, a Monday. Entries stop the day before.
TODAY = date(2026, 11, 16)

# (day, text, the one-line summary indexing would have written, notes it links)
ENTRIES = [
    (date(2026, 9, 28), "Started the running plan today. Ran 4 km before work, slow but fine. One coffee at 9. Slept well, about seven and a half hours.",
     "Started a running plan with a 4 km run; slept well.", []),
    (date(2026, 10, 1), "Ran 4 km. [[Luke]] came by in a state, the car needs a new clutch. Lent him £300. He says he'll pay it back by the 15th.",
     "Ran 4 km; lent Luke £300 for his car, to be repaid by Oct 15.", ["Luke"]),
    (date(2026, 10, 3), "Ran 5 km along the canal. Legs feel good. Quiet Saturday otherwise, batch cooked.",
     "Ran 5 km along the canal; quiet Saturday.", []),
    (date(2026, 10, 6), "Release week. Had an espresso at 5pm to push through the last tickets. Lay awake until 2am. Work is stressing me out, that's why I can't sleep.",
     "Release week; espresso at 5pm; awake until 2am, blamed work stress.", []),
    (date(2026, 10, 8), "Ran 5 km. No coffee after lunch today, just water. Slept eight hours straight.",
     "Ran 5 km; no afternoon coffee; slept eight hours.", []),
    (date(2026, 10, 10), "Ran 6 km. Farmers market with [[Priya]] after. Bought too much cheese.",
     "Ran 6 km; market with Priya.", ["Priya"]),
    (date(2026, 10, 13), "The dispatch routing bug finally got sorted. [[Ana]] found the cause, a timezone offset in the nightly batch job, and wrote the fix herself. I reviewed it and merged it this afternoon.",
     "Ana found and fixed the dispatch routing bug; I reviewed and merged it.", ["Ana"]),
    (date(2026, 10, 14), "Ran 5 km. At standup [[Tomas]] thanked me for fixing the routing bug in front of everyone. I said no problem and didn't mention [[Ana]]. She went quiet for the rest of the meeting. I think it's fine really, I merged it and it's all a team effort anyway.",
     "Ran 5 km; Tomas thanked me for the routing fix at standup and I didn't mention Ana.", ["Tomas", "Ana"]),
    (date(2026, 10, 15), "Cold brew with [[Priya]] at half past four, we talked for ages. Couldn't get to sleep until gone 1. I'm just a bad sleeper I suppose.",
     "Cold brew with Priya at 4:30pm; couldn't sleep until after 1am.", ["Priya"]),
    (date(2026, 10, 16), "[[Luke]] hasn't paid me back. Texted him, he says next week. Annoyed but didn't say so.",
     "Luke missed the repayment date and said next week.", ["Luke"]),
    (date(2026, 10, 17), "Ran 7 km, longest so far. Felt strong.",
     "Ran 7 km, the longest run so far.", []),
    (date(2026, 10, 20), "Brutal day. Audit prep from 8 till 7 and [[Tomas]] changed the scope twice. No coffee after noon because I forgot to buy any. Slept like a log, nine hours.",
     "Brutal day of audit prep; no coffee after noon; slept nine hours.", ["Tomas"]),
    (date(2026, 10, 21), "Ran 6 km. Tired legs.",
     "Ran 6 km on tired legs.", []),
    (date(2026, 10, 22), "Audit day. Two coffees after 4 to stay sharp for the auditors. Wide awake at 1:30am staring at the ceiling. The job is wrecking my sleep.",
     "Audit day; two coffees after 4pm; awake at 1:30am, blamed the job.", []),
    (date(2026, 10, 24), "Ran 8 km. Called mum after, she wants to know about Christmas.",
     "Ran 8 km; called mum.", []),
    (date(2026, 10, 27), "Planned Sunday's hike over dinner. [[Priya]] suggested the ridge route for the views. [[Luke]] said to take the valley path because of the forecast. We went with Luke's idea.",
     "Planned the hike: Priya suggested the ridge route, Luke the valley path; chose Luke's.", ["Priya", "Luke"]),
    (date(2026, 10, 28), "Ran 8 km before work. [[Tomas]] moved the client demo forward from Friday to tomorrow and only told me at 4pm. I haven't tested the import against the new data set. I didn't tell him that, I just said fine.",
     "Ran 8 km; Tomas moved the demo to tomorrow at short notice; import untested on new data and I didn't say so.", ["Tomas"]),
    (date(2026, 10, 29), "The demo went badly, the import fell over on the new data in front of the client. I argued with [[Tomas]] and told him it was his fault for moving the date. Properly wound up all evening. Had a herbal tea and went to bed at 10. Slept eight hours.",
     "Demo failed at the import; told Tomas it was his fault for moving the date; herbal tea, slept eight hours.", ["Tomas"]),
    (date(2026, 10, 30), "[[Luke]] paid back £100. Says the rest is coming.",
     "Luke repaid £100 of the £300.", ["Luke"]),
    (date(2026, 10, 31), "Ran 10 km! First double digits. So 18 km this week with Wednesday's 8.",
     "Ran 10 km, first double-digit run; 18 km for the week.", []),
    (date(2026, 11, 1), "New month. Goal: gym three times a week, every week, no excuses.",
     "Set a goal of going to the gym three times a week.", []),
    (date(2026, 11, 2), "Ran 10 km. Feeling invincible, going to push the distance this week.",
     "Ran 10 km and decided to push the distance.", []),
    (date(2026, 11, 3), "Gym after work, first session of the month. Flat white at 6 before the cinema with [[Priya]]. Film was good. Awake until 2 again. Work stress, it never switches off.",
     "Gym session; flat white at 6pm before the cinema; awake until 2am, blamed work stress.", ["Priya"]),
    (date(2026, 11, 4), "Ran 8 km.",
     "Ran 8 km.", []),
    (date(2026, 11, 5), "Ordinary day. Dentist tomorrow at 8, must remember to floss tonight.",
     "Ordinary day; dentist the next morning at 8.", []),
    (date(2026, 11, 6), "Dentist, all fine, no fillings. Ran 9 km in the evening.",
     "Dentist was fine; ran 9 km.", []),
    (date(2026, 11, 7), "[[Priya]]'s birthday dinner. I'd told her last week I'd have to leave by half nine because of the 6am train to mum's. Stayed two hours, gave her the book, left at 9:30 as planned. Felt guilty the whole way home. I think I was rude.",
     "Priya's birthday dinner; left at 9:30 as I'd told her I would; felt guilty.", ["Priya"]),
    (date(2026, 11, 8), "At mum's. Ran 12 km along the coast, so 39 km this week. Left shin started aching in the last kilometre.",
     "Ran 12 km at mum's, 39 km for the week; left shin started aching.", []),
    (date(2026, 11, 9), "[[Priya]] texted: thanks so much for coming, I love the book. Train home.",
     "Priya texted thanks for coming and for the book.", ["Priya"]),
    (date(2026, 11, 10), "Energy drink at 5 to get through the backlog. Bed at 11, asleep at 2. Same old story, the job.",
     "Energy drink at 5pm; didn't fall asleep until 2am, blamed the job.", []),
    (date(2026, 11, 11), "Shin is sore going down stairs. Probably nothing.",
     "Shin sore on stairs.", []),
    (date(2026, 11, 12), "Gym at lunch. Ran 10 km in the evening anyway, shin hurt from the start. [[Tomas]] wants the Q4 capacity report by Wednesday the 18th. I've only done the outline.",
     "Gym; ran 10 km on a sore shin; Tomas wants the Q4 capacity report by Wed Nov 18, only the outline is done.", ["Tomas"]),
    (date(2026, 11, 13), "Shin worse, it hurts to press on the bone. [[Luke]] asked to borrow another £500 for a deposit on a flat. I don't want to. I feel like a bad brother.",
     "Shin worse, sore to press; Luke asked to borrow another £500 and I don't want to.", ["Luke"]),
    (date(2026, 11, 14), "Feeling good about how consistent I've been with the gym this month. Promised mum I'd call on Tuesday evening about Christmas plans.",
     "Felt good about gym consistency; promised to call mum Tuesday evening.", []),
    (date(2026, 11, 15), "Shin still sore, skipped the run. Still haven't talked to [[Ana]] since the standup thing, it's been awkward. The report is still just an outline.",
     "Shin sore, skipped the run; still awkward with Ana; report still only an outline.", ["Ana"]),
]

# (title, kind, text)
NOTES = [
    ("Luke", "people", "My younger brother. Relationship: Brother. Borrowed £150 in 2025 and paid it back three months late."),
    ("Ana", "people", "Backend developer on my team. Relationship: Colleague. Joined in 2024."),
    ("Tomas", "people", "My manager. Relationship: Manager."),
    ("Priya", "people", "Friend since university. Relationship: Friend. Birthday 7 November."),
    ("Goals 2026", "goals", "Run a half marathon in March 2027. Gym three times a week. Be more honest at work. Save £2000 by the end of the year."),
    ("Sourdough", "hobbies", "Starter is called Gerald. Feed every morning. 75% hydration works best."),
]
