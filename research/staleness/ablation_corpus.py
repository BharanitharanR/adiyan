"""
2x2 ablation phrasings, extending research/staleness/corpus.py.

run_control.py established that the verbose correction is penalised, but
verbose differs from terse on TWO axes simultaneously:

    (a) it is longer
    (b) it restates the superseded value as a literal token ("not teal")

These two extra conditions complete the factorial so the axes can be
separated:

    short_mention - short, DOES restate the old value.  ("Crimson, not teal.")
    long_clean    - long, does NOT restate the old value. Padded with natural
                    elaboration carrying no reference to the superseded fact,
                    and length-matched to the verbose form as closely as
                    natural phrasing allows.

Giving:
                    | no old-value mention | mentions old value
        ------------+----------------------+--------------------
        short       | terse                | short_mention
        long        | long_clean           | verbose

Keyed by the query string so it joins cleanly onto CORPUS without
duplicating it.
"""

ABLATION = {
    'what is my favourite colour': dict(
        short_mention='Crimson, not teal.',
        long_clean='Having given it a fair amount of thought over time, I would say my favourite colour is crimson.'),
    'what kind of music do I like': dict(
        short_mention='Techno, not jazz.',
        long_clean='If someone asks me what I put on most evenings these days, the honest answer is that I like techno music.'),
    'what is my favourite cuisine': dict(
        short_mention='Korean, not Italian.',
        long_clean='Out of everything I have eaten over the past couple of years, my favourite cuisine is easily Korean.'),
    'which sport do I follow': dict(
        short_mention='Tennis, not cricket.',
        long_clean='When there is something on that I will actually rearrange my evening for, the sport I follow is tennis.'),
    'what is my preferred programming language': dict(
        short_mention='Rust, not Java.',
        long_clean='Given a free choice on a new project with no constraints imposed on me, my preferred programming language is Rust.'),

    'where do I work': dict(
        short_mention='Oracle, not Infosys.',
        long_clean='To answer the question people always ask at gatherings, I work at Oracle as of this year.'),
    'what is my job title': dict(
        short_mention='Principal engineer, not backend engineer.',
        long_clean='The title that appears on my formal paperwork and my email signature is principal engineer.'),
    'what team am I on': dict(
        short_mention='Platform team, not payments.',
        long_clean='In terms of where I actually sit day to day and who I attend standup with, I am on the platform team.'),

    'which city do I live in': dict(
        short_mention='Hyderabad, not Chennai.',
        long_clean='For anything involving post, deliveries or working out my commute, I live in Hyderabad.'),
    'where do I usually work from': dict(
        short_mention='Home, not the office.',
        long_clean='On a normal week without any particular meetings forcing my hand, I usually work from home.'),
    'which country am I travelling to': dict(
        short_mention='Portugal, not Japan.',
        long_clean='For the trip I have been slowly saving towards and have now finally booked, I am travelling to Portugal.'),

    'how many people are on my team': dict(
        short_mention='11 people, not 6.',
        long_clean='Counting everyone who attends our standup including the two recent joiners, there are 11 people on my team.'),
    'what is my monthly savings target': dict(
        short_mention='35000, not 20000.',
        long_clean='Working backwards from what I want to have put aside by the end of the year, my monthly savings target is 35000.'),
    'how many hours do I sleep': dict(
        short_mention='8 hours, not 5.',
        long_clean='Averaged across a normal working week rather than counting the occasional bad night, I sleep about 8 hours.'),

    'when is my weekly review': dict(
        short_mention='Thursday, not Monday.',
        long_clean='The slot I keep blocked out in my calendar for looking back over everything is on Thursday.'),
    'what time do I go to the gym': dict(
        short_mention='Morning, not evening.',
        long_clean='Fitting it around work in the way that has actually proved sustainable for me, I go to the gym in the morning.'),

    'am I currently reading anything': dict(
        short_mention='A physics book, not the habits one.',
        long_clean='On the small stack beside my bed that I am genuinely working through at the moment, I am reading a book about physics.'),
    'what is my dietary preference': dict(
        short_mention='Vegetarian, not unrestricted.',
        long_clean='For the purposes of ordering food or planning anything where catering matters, I am vegetarian.'),
    'do I own a car': dict(
        short_mention='No car, not like before.',
        long_clean='For anything involving parking, insurance or planning how I will get somewhere, I do not own a car.'),
    'what is my relationship to coffee': dict(
        short_mention='Tea, not coffee.',
        long_clean='As the thing I genuinely reach for first before doing anything else at the start of the day, I drink tea every morning.'),

    'which editor do I use': dict(
        short_mention='Neovim, not VS Code.',
        long_clean='For the great majority of the actual writing and editing I do on a given day, I use Neovim as my editor.'),
    'what database do I use': dict(
        short_mention='Postgres, not MySQL.',
        long_clean='For anything I am building where I get to make the call myself without inheriting a stack, I use Postgres.'),
    'which cloud provider do I use': dict(
        short_mention='Own hardware, not AWS.',
        long_clean='For everything I run personally rather than for work with someone else paying the bill, I deploy on my own hardware.'),

    'what am I trying to learn': dict(
        short_mention='Tamil, not Spanish.',
        long_clean='As the thing I am putting a genuinely regular twenty minutes a day into at the moment, I am trying to learn Tamil.'),
    'what is my fitness goal': dict(
        short_mention='Strength, not a marathon.',
        long_clean='Thinking about what I actually want to be capable of a year from now rather than a single event, my fitness goal is to build strength.'),
    'what is my main project': dict(
        short_mention='An agent harness, not the mobile app.',
        long_clean='Measured by where nearly all of my evenings and weekends have been going lately, my main project is an agent harness.'),

    'what is my preferred contact method': dict(
        short_mention='WhatsApp, not email.',
        long_clean='If something is genuinely time sensitive and you need a reply from me the same day, the best way to reach me is WhatsApp.'),
    'which bank do I use': dict(
        short_mention='ICICI, not HDFC.',
        long_clean='For my salary, my everyday spending and essentially all of my standing instructions, I bank with ICICI.'),
    'what is my notice period': dict(
        short_mention='90 days, not 30.',
        long_clean='According to the most recent contract I signed and what human resources has on file, my notice period is 90 days.'),
    'who is my emergency contact': dict(
        short_mention='My wife, not my brother.',
        long_clean='For the form they always ask you to fill in on the first day of anything, my emergency contact is my wife.'),
}
