import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

// Stub useInput so the overlay doesn't try to enter raw mode under renderSync
// (PassThrough stdin doesn't support it). Box/Text pass through to real Ink.
vi.mock('@hermes/ink', async importOriginal => {
  const mod = await importOriginal()

  return {
    ...mod,
    useInput: () => {}
  }
})

import type { SubscriptionOverlayState } from '../app/interfaces.js'
import { SubscriptionOverlay } from '../components/subscriptionOverlay.js'
import type { SubscriptionStateResponse } from '../gatewayTypes.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const t = DEFAULT_THEME

/** Render a SubscriptionOverlay to a string via renderSync + PassThrough. */
function render(overlay: SubscriptionOverlayState): string {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  let output = ''

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    React.createElement(SubscriptionOverlay, { onClose: () => {}, overlay, t }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  instance.unmount()
  instance.cleanup()

  return stripAnsi(output)
}

const state = (overrides: Partial<SubscriptionStateResponse> = {}): SubscriptionStateResponse => ({
  ok: true,
  logged_in: true,
  is_admin: true,
  can_change_plan: true,
  org_name: 'Acme',
  org_id: 'org_acme',
  role: 'OWNER',
  current: null,
  portal_url: 'https://portal.nousresearch.com/billing',
  ...overrides
})

const ctx = {
  openManageLink: vi.fn(() => Promise.resolve(true)),
  refreshState: vi.fn(() => Promise.resolve(null)),
  sys: vi.fn()
}

const overlay = (s: SubscriptionStateResponse): SubscriptionOverlayState => ({ ctx, screen: 'overview', state: s })

// Deep-link only: a single overview screen across every account state. No
// in-terminal tier picker, so there is no confirm/handoff screen to test.
describe('SubscriptionOverlay — overview', () => {
  it('free: upsell + "Start a subscription", no tier list, no "credits"', () => {
    const out = render(overlay(state({ current: null, usage: { available: true, status: 'free', plan_name: null } })))

    expect(out).toContain('Plan: Free · free models only')
    expect(out).toContain('Paid models need a subscription')
    expect(out).toContain('Start a subscription')
    expect(out).not.toContain('$20/mo')
    expect(out.toLowerCase()).not.toContain('credits')
  })

  it('subscriber: status line + plan bar + top-up bar, no "credits"', () => {
    const out = render(
      overlay(
        state({
          current: { tier_id: 'pro', tier_name: 'Pro', monthly_credits: '1000', credits_remaining: '700', cycle_ends_at: '2026-07-01', pending_downgrade_tier_name: null, pending_downgrade_at: null },
          usage: {
            available: true,
            status: 'healthy',
            plan_name: 'Pro',
            renews_display: 'Jul 1, 2026',
            total_spendable_display: '$26.00',
            has_topup: true,
            plan_bar: { kind: 'plan', remaining_display: '$14.00', total_display: '$20.00', spent_display: '$6.00', pct_used: 30, fill_fraction: 0.7 },
            topup_bar: { kind: 'topup', remaining_display: '$12.00', total_display: '$12.00', spent_display: '$0.00', pct_used: null, fill_fraction: 1 }
          }
        })
      )
    )

    expect(out).toContain('Plan: Pro')
    expect(out).toContain('$14.00 left of $20.00')
    expect(out).toContain('30% used')
    expect(out).toContain('top-up')
    expect(out).toContain('never expires')
    expect(out.toLowerCase()).not.toContain('credits')
  })

  it('low balance: shows alert nudge', () => {
    const out = render(
      overlay(
        state({
          current: { tier_id: 'pro', tier_name: 'Pro', monthly_credits: '1000', credits_remaining: '170', cycle_ends_at: '2026-07-01', pending_downgrade_tier_name: null, pending_downgrade_at: null },
          usage: { available: true, status: 'low', plan_name: 'Pro', total_spendable_display: '$3.40', plan_bar: { kind: 'plan', remaining_display: '$3.40', total_display: '$20.00', spent_display: '$16.60', pct_used: 83, fill_fraction: 0.17 } }
        })
      )
    )

    expect(out).toContain('Plan: Pro · $3.40 left')
    expect(out).toContain('Low balance')
  })

  it('not-admin: shows read-only note', () => {
    const out = render(
      overlay(
        state({
          is_admin: false,
          can_change_plan: false,
          role: 'MEMBER',
          current: { tier_id: 'pro', tier_name: 'Pro', monthly_credits: '1000', credits_remaining: '500', cycle_ends_at: '2026-07-01', pending_downgrade_tier_name: null, pending_downgrade_at: null },
          usage: { available: true, status: 'healthy', plan_name: 'Pro' }
        })
      )
    )

    expect(out).toContain('view only')
    expect(out).toContain('Manage on portal')
  })

  it('downgrade-pending: shows scheduled-switch banner (formatted date)', () => {
    const out = render(
      overlay(
        state({
          current: { tier_id: 'pro', tier_name: 'Pro', monthly_credits: '1000', credits_remaining: '500', cycle_ends_at: '2026-07-01', pending_downgrade_tier_name: 'Free', pending_downgrade_at: '2026-07-15', pending_downgrade_display: 'Jul 15, 2026' },
          usage: { available: true, status: 'healthy', plan_name: 'Pro' }
        })
      )
    )

    expect(out).toContain('Scheduled to switch to Free')
    expect(out).toContain('Jul 15, 2026')
  })

  it('team context: redirects to /topup, no tier picker', () => {
    const out = render(overlay(state({ context: 'team', current: null })))

    expect(out).toContain('shared balance')
    expect(out).toContain('/topup')
  })
})
