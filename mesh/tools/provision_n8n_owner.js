#!/usr/bin/env node
/**
 * Pre-provisions n8n's owner account so a fresh Adiyan install never shows
 * n8n's own interactive "Set up owner account" browser wizard - the same
 * reasoning register_webhook.py's own docstring already documents for the
 * WhatsApp webhook: a business owner installing this mesh should never need
 * to manually click through a third-party tool's first-run setup.
 *
 * Idempotent by design: if credentials already exist on disk (from a
 * previous run), this reuses them rather than regenerating - n8n's owner
 * account, once created, keeps that email/password forever; silently
 * rotating the hash on every mesh restart would lock out anyone who
 * actually did log into the editor UI once.
 *
 * n8n reads N8N_INSTANCE_OWNER_MANAGED_BY_ENV=true plus
 * N8N_INSTANCE_OWNER_EMAIL/_FIRST_NAME/_LAST_NAME/_PASSWORD_HASH at
 * startup and creates the owner account from them directly - no browser
 * step at all. The hash MUST be bcrypt (a plaintext password in that env
 * var silently breaks login, per n8n's own docs) - uses n8n's own bundled
 * bcryptjs dependency rather than adding a new one, so this has zero
 * install footprint beyond n8n itself.
 *
 * Two output files, both under ~/.Adiyan/, neither ever logged or printed
 * in full:
 *   - n8n_owner_credentials.json: the real, plaintext password, for the
 *     deployer's own reference if they ever want to log into the editor
 *     UI by hand. Not read by n8n itself.
 *   - n8n_owner.env: KEY=VALUE lines (hash only, never plaintext) meant to
 *     be sourced by whatever shell command actually launches `n8n start`.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const ADIYAN_HOME = path.join(os.homedir(), '.Adiyan');
const CREDENTIALS_PATH = path.join(ADIYAN_HOME, 'n8n_owner_credentials.json');
const ENV_PATH = path.join(ADIYAN_HOME, 'n8n_owner.env');

function findBcrypt() {
  // n8n's own bundled copy - see this file's own docstring for why this
  // isn't a fresh dependency of its own. `npm root -g` is the portable way
  // to find where a global install actually landed (varies by machine/nvm
  // setup) - a hardcoded path guess broke on the very first run here.
  try {
    const globalRoot = execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim();
    return require(path.join(globalRoot, 'n8n', 'node_modules', 'bcryptjs'));
  } catch (e) {
    // Fall back to a plain resolution, in case n8n's own dependency layout
    // or the global root ever differs - never silently fail provisioning
    // over this alone.
    return require('bcryptjs');
  }
}

function generatePassword() {
  // n8n's own rule: 8+ characters, at least 1 number, 1 capital letter.
  // Built from a fixed safe alphabet (no quote/backslash/space characters)
  // so this can round-trip through a KEY=VALUE env file with zero escaping
  // needed - a generated password containing a literal `"` or newline
  // would otherwise corrupt n8n_owner.env for every line after it.
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  const bytes = crypto.randomBytes(20);
  let password = 'A2'; // guarantees the capital-letter and digit requirement up front
  for (let i = 0; i < bytes.length; i++) {
    password += alphabet[bytes[i] % alphabet.length];
  }
  return password;
}

function main() {
  fs.mkdirSync(ADIYAN_HOME, { recursive: true });

  let credentials;
  if (fs.existsSync(CREDENTIALS_PATH)) {
    credentials = JSON.parse(fs.readFileSync(CREDENTIALS_PATH, 'utf8'));
  } else {
    const bcrypt = findBcrypt();
    const password = generatePassword();
    credentials = {
      email: 'owner@adiyan.local',
      firstName: 'Adiyan',
      lastName: 'Owner',
      password,
      passwordHash: bcrypt.hashSync(password, 10),
      createdAt: new Date().toISOString(),
    };
    fs.writeFileSync(CREDENTIALS_PATH, JSON.stringify(credentials, null, 2), { mode: 0o600 });
    console.error(`Provisioned a new n8n owner account - credentials saved to ${CREDENTIALS_PATH}`);
  }

  // Single-quoted values - a bcrypt hash's own literal `$` characters
  // (`$2a$10$...`) look like positional-parameter expansions to bash the
  // moment this file is `source`d unquoted (confirmed live: n8n rejected
  // the resulting mangled hash outright as "not a valid bcrypt hash").
  // Single quotes are the one quoting style bash treats as fully literal
  // even while sourcing a file as a script, so this is the only form safe
  // for the launch command in start_all.sh to `source` directly.
  const envLines = [
    "N8N_INSTANCE_OWNER_MANAGED_BY_ENV='true'",
    `N8N_INSTANCE_OWNER_EMAIL='${credentials.email}'`,
    `N8N_INSTANCE_OWNER_FIRST_NAME='${credentials.firstName}'`,
    `N8N_INSTANCE_OWNER_LAST_NAME='${credentials.lastName}'`,
    `N8N_INSTANCE_OWNER_PASSWORD_HASH='${credentials.passwordHash}'`,
  ];
  fs.writeFileSync(ENV_PATH, envLines.join('\n') + '\n', { mode: 0o600 });
}

main();
