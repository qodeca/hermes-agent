import { Box, Text, useInput } from '@hermes/ink'
import { useState } from 'react'

import type { SubscriptionOverlayState } from '../app/interfaces.js'
import type { SubscriptionStateResponse } from '../gatewayTypes.js'
import type { Theme } from '../theme.js'

import { footer, MenuRow, UsageBars } from './overlayPrimitives.js'

interface SubscriptionOverlayProps {
  /** Close the overlay entirely. */
  onClose: () => void
  overlay: SubscriptionOverlayState
  t: Theme
}

/**
 * The /subscription modal — deep-link only, NEVER charges in-terminal. A single
 * overview screen (plan + usage) that hands off to the portal in the browser to
 * change the plan: no in-terminal picker, no step-up (the scope gate lives on
 * /topup's charge). All RPCs live in subscription.ts, reached via `overlay.ctx`.
 */
export function SubscriptionOverlay({ onClose, overlay, t }: SubscriptionOverlayProps) {
  const { ctx, state: s } = overlay

  return (
    <Box borderColor={t.color.accent} borderStyle="round" flexDirection="column" paddingX={1}>
      {s.context === 'team' ? (
        <TeamContextScreen onClose={onClose} s={s} t={t} />
      ) : (
        <OverviewScreen ctx={ctx} onClose={onClose} s={s} t={t} />
      )}
    </Box>
  )
}

// ── Screen: Overview (covers states a–e: free/mid/top/not-admin/downgrade) ──

interface ScreenProps {
  ctx: SubscriptionOverlayState['ctx']
  onClose: () => void
  s: SubscriptionStateResponse
  t: Theme
}

/** Status line — dollars-only, state-matched. The allowance detail ($X of $Y ·
 *  % used) lives on the bar line, so this line shows only the spendable total +
 *  renewal — no duplicated "of $Y left". */
function statusLine(s: SubscriptionStateResponse): string {
  const u = s.usage
  const plan = s.current?.tier_name ?? u?.plan_name ?? null
  const renewsRaw = u?.renews_display ?? null
  const renews = renewsRaw ? ` · renews ${renewsRaw}` : ''
  const viewOnly = !s.can_change_plan

  if (!plan) {
    return 'Plan: Free · free models only'
  }

  if (u?.status === 'low' && u.total_spendable_display) {
    return `Plan: ${plan} · ${u.total_spendable_display} left`
  }

  // Healthy/top: show the spendable total once; the bar carries the breakdown.
  const left = u?.total_spendable_display ? ` · ${u.total_spendable_display} left` : ''

  return `Plan: ${plan}${left}${viewOnly ? ' · view only' : renews}`
}

function OverviewScreen({ ctx, onClose, s, t }: ScreenProps) {
  const c = s.current
  const isFree = !c?.tier_id
  const isCancelScheduled = !!c?.cancel_at_period_end
  const hasPendingDowngrade = !!c?.pending_downgrade_tier_name

  // Headline precedence: cancel-scheduled > downgrade-pending > active.
  // (Past-due/dunning was removed from the NAS read — a card-failing
  // subscriber now returns as a normal plan; no special-casing here.)
  const cancelOn = c?.cancellation_effective_display ?? c?.cancellation_effective_at

  const cancellationNote = isCancelScheduled
    ? cancelOn
      ? `Cancels on ${cancelOn} — your plan stays active until then.`
      : 'Cancellation scheduled — your plan stays active until the end of the billing period.'
    : null

  const downgradeOn =
    c?.pending_downgrade_display ?? c?.pending_downgrade_at ?? 'the end of the billing period'

  const downgradeNote =
    !isCancelScheduled && hasPendingDowngrade
      ? `Scheduled to switch to ${c?.pending_downgrade_tier_name} on ${downgradeOn}.`
      : null

  // State-matched upsell/alert nudge (dollars-only; healthy stays silent).
  const u = s.usage
  const freeNudge = isFree ? 'Paid models need a subscription. Start one to reach them.' : null

  const lowNudge =
    u?.status === 'low'
      ? `Low balance · ${u.total_spendable_display ?? 'under $5'} left. Top up or upgrade before a mid-run cutoff.`
      : null

  // No in-terminal plan picker (decided with the user): just show usage + the
  // current plan, then hand off to the portal to manage it. Members (non-admin)
  // get the same two actions — the portal enforces who can actually change it.
  // Free users have nothing to "manage" yet, so the verb matches their job.
  const manageLabel = isFree ? 'Start a subscription' : 'Manage on portal'
  const items = [manageLabel, 'Close']
  const [sel, setSel] = useState(0)

  const choose = (i: number) => {
    if (i === 0) {
      if (s.portal_url) {
        ctx.sys('Opening your subscription page in the browser…')
        void ctx.openManageLink()
      } else {
        ctx.sys('🔴 No portal URL available — manage your subscription on the Nous portal.')
      }
    }

    return onClose()
  }

  useInput((ch, key) => {
    if (key.escape) {
      return onClose()
    }

    if (key.upArrow && sel > 0) {
      setSel(v => v - 1)
    }

    if (key.downArrow && sel < items.length - 1) {
      setSel(v => v + 1)
    }

    if (key.return) {
      return choose(sel)
    }

    const n = parseInt(ch, 10)

    if (n >= 1 && n <= items.length) {
      return choose(n - 1)
    }
  })

  return (
    <Box flexDirection="column">
      <Text bold color={t.color.accent}>
        {statusLine(s)}
      </Text>
      <UsageBars model={s.usage} t={t} />
      {freeNudge && (
        <Box marginTop={1}>
          <Text color={t.color.warn}>
            {'> '}
            {freeNudge}
          </Text>
        </Box>
      )}
      {lowNudge && (
        <Box marginTop={1}>
          <Text color={t.color.warn}>
            {'! '}
            {lowNudge}
          </Text>
        </Box>
      )}
      {s.org_name && (
        <Text color={t.color.muted}>
          Org: {s.org_name}
          {s.role ? ` · ${s.role}` : ''}
        </Text>
      )}
      {cancellationNote && (
        <Box marginTop={1}>
          <Text color={t.color.warn}>{cancellationNote}</Text>
        </Box>
      )}
      {downgradeNote && (
        <Box marginTop={1}>
          <Text color={t.color.warn}>{downgradeNote}</Text>
        </Box>
      )}

      <Text />
      {items.map((label, i) => (
        <MenuRow active={sel === i} index={i + 1} key={label} label={label} t={t} />
      ))}

      <Text />
      {footer('↑/↓ select · Enter confirm · Esc close', t)}
    </Box>
  )
}

// ── Screen: Team context (no tier picker — teams use shared credits) ──

interface TeamContextScreenProps {
  onClose: () => void
  s: SubscriptionStateResponse
  t: Theme
}

function TeamContextScreen({ onClose, s, t }: TeamContextScreenProps) {
  useInput((_ch, key) => {
    if (key.escape || key.return) {
      return onClose()
    }
  })

  return (
    <Box flexDirection="column">
      <Text bold color={t.color.accent}>
        Team subscription
      </Text>
      {s.org_name && (
        <Text color={t.color.muted}>
          Org: {s.org_name}
          {s.role ? ` · ${s.role}` : ''}
        </Text>
      )}
      <Text />
      <Text color={t.color.text}>
        This terminal is connected to {s.org_name ?? 'a team org'}. Teams run on a shared balance · use /topup to add
        funds.
      </Text>
      <Text color={t.color.muted}>Personal subscriptions live on your personal account.</Text>

      <Text />
      {footer('Enter/Esc close', t)}
    </Box>
  )
}
