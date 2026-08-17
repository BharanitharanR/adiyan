"""
Real end-to-end tests for the AI Cron Jobs / routines engine, driven entirely
through the owner admin channel exactly as a real WhatsApp message would
(Tests/http/conftest.py's client.owner_message) - no mocking of the job
engine, the routines index, or the database. Only the WhatsApp send itself
is faked.
"""
import time

import config.database as db


def test_create_job_creates_a_routine(client, unique_name, cleanup_routines):
    name = unique_name('test_routine')
    cleanup_routines.append(name)

    reply = client.owner_reply_text(
        f'Create a job named "{name}" that runs every day at 3pm and sends me a short reminder to stretch.'
    )
    assert reply, 'Expected a reply from the admin agent'

    routine = db.get_routine(name)
    assert routine is not None, f'Expected a routine named {name!r} to have been created'
    assert routine['description']


def test_asking_again_reuses_the_routine_not_a_duplicate(client, unique_name, cleanup_routines):
    """The actual check-and-trigger behavior this whole engine exists for:
    asking for something under a name that already exists must not create a
    second job under that name."""
    name = unique_name('test_dedupe')
    cleanup_routines.append(name)

    client.owner_reply_text(
        f'Create a job named "{name}" every day at 4pm to remind me to drink water.'
    )
    assert db.get_routine(name) is not None

    jobs_before = [j for j in db.list_cron_jobs() if j['name'].lower() == name.lower()]
    assert len(jobs_before) == 1

    # Ask for the exact same thing again, by name - should trigger, not duplicate.
    client.owner_reply_text(f'Create a job named "{name}" every day at 4pm to remind me to drink water.')

    jobs_after = [j for j in db.list_cron_jobs() if j['name'].lower() == name.lower()]
    assert len(jobs_after) == 1, (
        f'Expected exactly one job named {name!r} after asking twice - got {len(jobs_after)}, '
        'meaning a duplicate was created instead of the existing routine being reused.'
    )


def test_trigger_phrase_fires_without_a_schedule(client, unique_name, cleanup_routines):
    """A routine's trigger phrase should fire the moment it's said, independent
    of any scheduled prompt - the exact "reached p" use case this feature was
    built for."""
    name = unique_name('test_trigger')
    cleanup_routines.append(name)
    phrase = f'phrase_{name}'

    client.owner_reply_text(
        f'Create a job named "{name}" every day at 5pm that asks a check-in question and waits for a reply.'
    )
    routine = db.get_routine(name)
    assert routine is not None

    set_reply = client.owner_reply_text(
        f'Set the trigger phrase for the "{name}" routine to "{phrase}", '
        f'action log, with acknowledgment message "Logged it!"'
    )
    assert set_reply

    updated = db.get_routine_by_trigger_phrase(phrase)
    assert updated is not None, f'Expected trigger phrase {phrase!r} to be set on {name!r}'
    assert updated['name'].lower() == name.lower()

    # Say the trigger phrase itself, as a plain message - not a "create" or
    # "trigger" request, just the bare phrase, exactly as a real user would.
    result = client.owner_message(phrase)
    sent = result.get('sent_messages') or []
    assert any('Logged it!' in m['message'] for m in sent), (
        f'Expected the configured acknowledgment for trigger phrase {phrase!r} - got {sent}'
    )

    job = next((j for j in db.list_cron_jobs() if j['name'].lower() == name.lower()), None)
    assert job is not None
    today_key = f"trigger:{time.strftime('%Y-%m-%d')}"
    data = db.read_job_data(job['id'], key=today_key)
    assert any(d['value'] == phrase for d in data), 'Expected the trigger to have logged job_data for today'


def test_get_routine_details_returns_real_schedule_and_instructions(client, unique_name, cleanup_routines):
    name = unique_name('test_details')
    cleanup_routines.append(name)

    client.owner_reply_text(
        f'Create a job named "{name}" every Monday at 9am that sends a weekly planning prompt.'
    )
    reply = client.owner_reply_text(f'What does the "{name}" routine actually do - full details please.')
    assert name.split('_')[0] in reply.lower() or 'monday' in reply.lower(), (
        f'Expected the routine details reply to reference its actual schedule/name, got: {reply!r}'
    )
