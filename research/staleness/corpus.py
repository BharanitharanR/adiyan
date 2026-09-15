"""
Fact/correction corpus for the semantic-staleness experiment.

Each item is one fact that a user later corrects. The question under test:
when a memory store retains BOTH the original statement and the correction
(which mem0ai and similar stores do - they add rather than replace), does
cosine similarity against a natural query rank the CORRECTION above the
STALE original?

Three correction phrasings per item, to separate length from semantics:

  correction_terse    - same structure/length as the original, new value only.
                        If the effect is purely semantic, this should win.
  correction_matched  - natural standalone phrasing, moderately longer.
  correction_verbose  - conversational correction that also NAMES the old
                        value ("actually X, not Y"). This is what real users
                        type, and it is the adversarial case: it is longer
                        AND it contains the stale value as a token.

Deliberately includes domains where the effect is expected to be weak
(numeric/date corrections, where the changed token is small relative to the
sentence) so this is a fair test rather than a demonstration.
"""

CORPUS = [
    # ---------- preferences (changed value is a content word) ----------
    dict(domain='preference', query='what is my favourite colour',
         original='My favourite colour is teal.',
         terse='My favourite colour is crimson.',
         matched='I prefer the colour crimson these days.',
         verbose='Actually my favourite colour is crimson, not teal.'),
    dict(domain='preference', query='what kind of music do I like',
         original='I like jazz music.',
         terse='I like techno music.',
         matched='These days I mostly listen to techno.',
         verbose='I used to say jazz but really I like techno now, not jazz.'),
    dict(domain='preference', query='what is my favourite cuisine',
         original='My favourite cuisine is Italian.',
         terse='My favourite cuisine is Korean.',
         matched='Korean food is what I enjoy most now.',
         verbose='Correction: my favourite cuisine is Korean, not Italian.'),
    dict(domain='preference', query='which sport do I follow',
         original='I follow cricket.',
         terse='I follow tennis.',
         matched='Tennis is the sport I keep up with.',
         verbose='I do not really follow cricket anymore, I follow tennis.'),
    dict(domain='preference', query='what is my preferred programming language',
         original='My preferred programming language is Java.',
         terse='My preferred programming language is Rust.',
         matched='I mostly write Rust now by choice.',
         verbose='Update: my preferred programming language is Rust, no longer Java.'),

    # ---------- roles / employment ----------
    dict(domain='role', query='where do I work',
         original='I work at Infosys.',
         terse='I work at Oracle.',
         matched='Oracle is my current employer.',
         verbose='I moved on from Infosys, I work at Oracle now.'),
    dict(domain='role', query='what is my job title',
         original='I am a backend engineer.',
         terse='I am a principal engineer.',
         matched='My current title is principal engineer.',
         verbose='I was a backend engineer but I am a principal engineer now.'),
    dict(domain='role', query='what team am I on',
         original='I am on the payments team.',
         terse='I am on the platform team.',
         matched='The platform team is where I sit now.',
         verbose='I switched off payments, I am on the platform team these days.'),

    # ---------- location ----------
    dict(domain='location', query='which city do I live in',
         original='I live in Chennai.',
         terse='I live in Hyderabad.',
         matched='Hyderabad is where I am based now.',
         verbose='I relocated from Chennai, I live in Hyderabad now.'),
    dict(domain='location', query='where do I usually work from',
         original='I usually work from the office.',
         terse='I usually work from home.',
         matched='Working from home is my normal setup now.',
         verbose='I no longer work from the office, I usually work from home.'),
    dict(domain='location', query='which country am I travelling to',
         original='I am travelling to Japan.',
         terse='I am travelling to Portugal.',
         matched='Portugal is my upcoming destination.',
         verbose='The Japan trip fell through, I am travelling to Portugal instead.'),

    # ---------- numeric (changed token is small - effect expected weaker) ----------
    dict(domain='numeric', query='how many people are on my team',
         original='There are 6 people on my team.',
         terse='There are 11 people on my team.',
         matched='My team has grown to 11 people.',
         verbose='It is not 6 anymore, there are 11 people on my team.'),
    dict(domain='numeric', query='what is my monthly savings target',
         original='My monthly savings target is 20000.',
         terse='My monthly savings target is 35000.',
         matched='I now aim to save 35000 each month.',
         verbose='I raised it from 20000, my monthly savings target is 35000.'),
    dict(domain='numeric', query='how many hours do I sleep',
         original='I sleep about 5 hours a night.',
         terse='I sleep about 8 hours a night.',
         matched='I get around 8 hours of sleep now.',
         verbose='I used to sleep 5 hours, now I sleep about 8 hours a night.'),

    # ---------- dates / schedule ----------
    dict(domain='date', query='when is my weekly review',
         original='My weekly review is on Monday.',
         terse='My weekly review is on Thursday.',
         matched='Thursday is when the weekly review happens.',
         verbose='The weekly review moved off Monday, it is on Thursday now.'),
    dict(domain='date', query='what time do I go to the gym',
         original='I go to the gym in the evening.',
         terse='I go to the gym in the morning.',
         matched='Morning gym sessions are my routine now.',
         verbose='I stopped going in the evening, I go to the gym in the morning.'),

    # ---------- status / state ----------
    dict(domain='status', query='am I currently reading anything',
         original='I am reading a book about habits.',
         terse='I am reading a book about physics.',
         matched='My current book is about physics.',
         verbose='I finished the habits book, I am reading one about physics now.'),
    dict(domain='status', query='what is my dietary preference',
         original='I eat everything, no restrictions.',
         terse='I am vegetarian.',
         matched='I follow a vegetarian diet now.',
         verbose='I used to eat everything but I am vegetarian now.'),
    dict(domain='status', query='do I own a car',
         original='I own a car.',
         terse='I do not own a car.',
         matched='I sold my car and use public transport.',
         verbose='I used to own a car but I do not own a car anymore.'),
    dict(domain='status', query='what is my relationship to coffee',
         original='I drink coffee every morning.',
         terse='I drink tea every morning.',
         matched='Tea has replaced coffee in my mornings.',
         verbose='I quit coffee, I drink tea every morning instead of coffee.'),

    # ---------- tools / systems ----------
    dict(domain='tools', query='which editor do I use',
         original='I use VS Code as my editor.',
         terse='I use Neovim as my editor.',
         matched='Neovim is my editor of choice now.',
         verbose='I switched away from VS Code, I use Neovim as my editor.'),
    dict(domain='tools', query='what database do I use',
         original='I use MySQL for my projects.',
         terse='I use Postgres for my projects.',
         matched='Postgres is the database I build on now.',
         verbose='I migrated off MySQL, I use Postgres for my projects now.'),
    dict(domain='tools', query='which cloud provider do I use',
         original='I deploy on AWS.',
         terse='I deploy on my own hardware.',
         matched='Self-hosting on my own hardware is how I deploy now.',
         verbose='I left AWS behind, I deploy on my own hardware these days.'),

    # ---------- goals ----------
    dict(domain='goal', query='what am I trying to learn',
         original='I am trying to learn Spanish.',
         terse='I am trying to learn Tamil.',
         matched='Tamil is the language I am studying now.',
         verbose='I paused Spanish, I am trying to learn Tamil at the moment.'),
    dict(domain='goal', query='what is my fitness goal',
         original='My fitness goal is to run a marathon.',
         terse='My fitness goal is to build strength.',
         matched='Building strength is what I am working towards.',
         verbose='I gave up on the marathon, my fitness goal is to build strength.'),
    dict(domain='goal', query='what is my main project',
         original='My main project is a mobile app.',
         terse='My main project is an agent harness.',
         matched='I am mostly building an agent harness now.',
         verbose='The mobile app is shelved, my main project is an agent harness.'),

    # ---------- contact / admin ----------
    dict(domain='admin', query='what is my preferred contact method',
         original='The best way to reach me is email.',
         terse='The best way to reach me is WhatsApp.',
         matched='WhatsApp is how I prefer to be contacted.',
         verbose='Do not use email, the best way to reach me is WhatsApp.'),
    dict(domain='admin', query='which bank do I use',
         original='I bank with HDFC.',
         terse='I bank with ICICI.',
         matched='ICICI is my current bank.',
         verbose='I closed my HDFC account, I bank with ICICI now.'),
    dict(domain='admin', query='what is my notice period',
         original='My notice period is 30 days.',
         terse='My notice period is 90 days.',
         matched='I am now on a 90 day notice period.',
         verbose='It changed from 30 days, my notice period is 90 days.'),
    dict(domain='admin', query='who is my emergency contact',
         original='My emergency contact is my brother.',
         terse='My emergency contact is my wife.',
         matched='My wife is listed as my emergency contact.',
         verbose='Change it from my brother, my emergency contact is my wife.'),
]
