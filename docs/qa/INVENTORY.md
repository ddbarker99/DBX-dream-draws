# DBX Dream Draws — platform inventory

Generated from the code by `tests/qa_inventory.py`. Regenerate after any change.

## Routes — Admin (39)

| URL | Methods | Endpoint | Protected by |
|---|---|---|---|
| `/admin/announcements` | GET,POST | `admin.announcements` | staff: `settings` |
| `/admin/audit` | GET | `admin.audit_log` | staff: `audit` |
| `/admin/competitions` | GET | `admin.dashboard` | staff: `comps.view` |
| `/admin/competitions/<int:cid>` | GET | `admin.entries` | staff: `comps.view` |
| `/admin/competitions/<int:cid>/delete` | POST | `admin.delete_comp` | staff: `comps` |
| `/admin/competitions/<int:cid>/draw` | POST | `admin.draw` | staff: `draws` |
| `/admin/competitions/<int:cid>/edit` | GET,POST | `admin.edit_competition` | staff: `comps` |
| `/admin/competitions/<int:cid>/export.csv` | GET | `admin.export` | staff: `comps.view` |
| `/admin/competitions/<int:cid>/instant` | POST | `admin.instant` | staff: `comps` |
| `/admin/competitions/<int:cid>/postal` | POST | `admin.postal` | staff: `postal` |
| `/admin/competitions/<int:cid>/redraw` | POST | `admin.redraw_comp` | staff: `draws` |
| `/admin/competitions/<int:cid>/status` | POST | `admin.set_status` | staff: `comps` |
| `/admin/competitions/<int:cid>/winner` | POST | `admin.winner_story` | staff: `prizes` |
| `/admin/competitions/delete-selected` | POST | `admin.delete_selected` | staff: `comps` |
| `/admin/competitions/new` | GET,POST | `admin.new_competition` | staff: `comps` |
| `/admin/content` | GET | `admin.content_list` | staff: `settings` |
| `/admin/content/<slug>` | GET,POST | `admin.content_edit` | staff: `settings` |
| `/admin/deposit-refunds/<int:did>/done` | POST | `admin.deposit_refund_done` | staff: `money` |
| `/admin/evidence/<name>` | GET | `admin.evidence` | staff: `prizes` |
| `/admin/features` | GET,POST | `admin.features` | staff: `settings` |
| `/admin/games/free-daily` | POST | `admin.free_daily_game` | staff: `comps` |
| `/admin/games/publish-drafts` | POST | `admin.publish_draft_games` | staff: `comps` |
| `/admin/games/random` | POST | `admin.random_games` | staff: `comps` |
| `/admin/games/starter` | POST | `admin.starter_games` | staff: `comps` |
| `/admin/instant/<int:pid>/fulfilled` | POST | `admin.instant_fulfilled` | staff: `prizes` |
| `/admin/orders/<int:cid>` | GET,POST | `admin.order_detail` | staff: `users.view` |
| `/admin/payouts` | GET,POST | `admin.payouts` | staff: `money` |
| `/admin/payouts/export.csv` | GET | `admin.payouts_csv` | staff: `money` |
| `/admin/postal` | GET | `admin.postal_queue` | staff: `postal` |
| `/admin/postal/<int:pid>` | POST | `admin.postal_process` | staff: `postal` |
| `/admin/prizes` | GET | `admin.prizes` | staff: `prizes` |
| `/admin/prizes/<int:claim_id>` | GET,POST | `admin.claim_detail` | staff: `prizes` |
| `/admin/promos` | GET,POST | `admin.promos` | staff: `money` |
| `/admin/refunds/<int:cid>/done` | POST | `admin.refund_done` | staff: `money` |
| `/admin/settings` | GET,POST | `admin.settings` | staff: `settings` |
| `/admin/start-fresh` | GET,POST | `admin.reset` | staff: `reset` |
| `/admin/stats` | GET | `admin.stats` | staff: `reports` |
| `/admin/users` | GET | `admin.users` | staff: `users.view` |
| `/admin/users/<int:uid>` | GET,POST | `admin.user_detail` | staff: `users.view` |

## Routes — Admin — Control Centre & reports (32)

| URL | Methods | Endpoint | Protected by |
|---|---|---|---|
| `/admin/` | GET | `control.centre` | staff: `comps.view` |
| `/admin/analytics` | GET | `control.analytics` | staff: `reports` |
| `/admin/approvals` | GET,POST | `control.approvals` | staff: `comps.view` |
| `/admin/backlog` | GET,POST | `control.backlog` | staff: `cases` |
| `/admin/backlog/<int:bid>` | GET | `control.backlog_item` | staff: `cases` |
| `/admin/calendar` | GET | `control.ops_calendar` | staff: `comps.view` |
| `/admin/cases` | GET | `control.cases` | staff: `cases` |
| `/admin/cases/<int:case_id>` | GET,POST | `control.case_detail` | staff: `cases` |
| `/admin/checkout-diagnostics` | GET | `control.checkout_diagnostics` | staff: `reports` |
| `/admin/communications` | GET | `control.communications` | staff: `users.view` |
| `/admin/communications/retry` | POST | `control.communications_retry` | staff: `users.view` |
| `/admin/compliance` | GET,POST | `control.compliance` | staff: `comps.view` |
| `/admin/customers/<int:uid>/timeline` | GET | `control.timeline` | staff: `users.view` |
| `/admin/emails` | GET | `control.email_previews` | staff: `settings` |
| `/admin/emergency` | GET,POST | `control.emergency` | staff: `settings` |
| `/admin/errors/<int:eid>/resolve` | POST | `control.resolve_error` | staff: `audit` |
| `/admin/experiments` | GET,POST | `control.experiments` | staff: `reports` |
| `/admin/feedback` | GET | `control.feedback_admin` | staff: `cases` |
| `/admin/finance` | GET | `control.finance` | staff: `reports` |
| `/admin/finance/payments.csv` | GET | `control.finance_csv` | staff: `reports` |
| `/admin/flags` | GET,POST | `control.flags` | staff: `flags` |
| `/admin/health` | GET,POST | `control.health` | staff: `audit` |
| `/admin/liability` | GET | `control.liability` | staff: `reports` |
| `/admin/performance` | GET | `control.performance` | staff: `reports` |
| `/admin/releases` | GET | `control.releases` | staff: `audit` |
| `/admin/reports` | GET,POST | `control.reports` | staff: `reports` |
| `/admin/risk` | GET | `control.risk` | staff: `audit` |
| `/admin/risk/after-draw/<int:aid>/resolve` | POST | `control.resolve_after_draw` | staff: `audit` |
| `/admin/search` | GET | `control.search` | staff: `comps.view` |
| `/admin/segments` | GET | `control.segments` | staff: `reports` |
| `/admin/support-insights` | GET | `control.support_insights` | staff: `cases` |
| `/admin/targets` | GET,POST | `control.targets` | staff: `reports` |

## Routes — Customer-facing (public + account) (84)

| URL | Methods | Endpoint | Protected by |
|---|---|---|---|
| `/` | GET | `public.home` | public |
| `/<any("free-entry","terms","fair-draws","faq","responsible-play","complaints","privacy","cookies","about"):page>` | GET | `public.page` | public |
| `/account` | GET | `public.account` | logged in |
| `/account/activity` | GET | `public.account_activity_page` | logged in |
| `/account/deposit` | POST | `public.deposit` | logged in |
| `/account/deposit/refund` | POST | `public.deposit_refund` | logged in |
| `/account/exclude` | POST | `public.self_exclude` | logged in |
| `/account/export/<kind>.csv` | GET | `public.account_export` | logged in |
| `/account/export/everything.json` | GET | `public.account_export_all` | logged in |
| `/account/limits` | POST | `public.set_limits` | logged in |
| `/account/notifications` | GET | `public.notifications` | logged in |
| `/account/notifications/<int:nid>/open` | GET | `public.notification_open` | logged in |
| `/account/notifications/read` | POST | `public.notifications_read` | logged in |
| `/account/orders/<int:cid>` | GET | `public.order_detail` | logged in |
| `/account/preferences` | POST | `public.preferences` | logged in |
| `/account/prizes/<int:claim_id>` | GET,POST | `public.prize_claim` | logged in |
| `/account/profile` | POST | `public.profile` | logged in |
| `/account/redeem` | POST | `public.redeem` | logged in |
| `/account/resend-verification` | POST | `public.resend_verification` | logged in |
| `/account/sessions/revoke` | POST | `public.revoke_session` | logged in |
| `/account/tickets/<int:tid>` | GET | `public.ticket_detail` | logged in |
| `/account/withdraw` | POST | `public.withdraw` | logged in |
| `/account/withdrawals/<int:wid>` | GET | `public.withdrawal_detail` | logged in |
| `/api/competitions` | GET | `public.api_competitions` | public |
| `/basket` | GET | `public.basket` | public |
| `/basket/add` | POST | `public.basket_add` | public |
| `/basket/checkout` | POST | `public.checkout` | logged in |
| `/basket/promo` | POST | `public.basket_promo` | public |
| `/basket/remove` | POST | `public.basket_remove` | public |
| `/basket/update` | POST | `public.basket_update` | public |
| `/c/<slug>` | GET | `public.competition` | public |
| `/c/<slug>/conditions/<int:version>` | GET | `public.comp_conditions` | public |
| `/c/<slug>/draw` | GET | `public.draw_verify` | public |
| `/c/<slug>/draw/entries.txt` | GET | `public.draw_entries` | public |
| `/c/<slug>/entries` | GET | `public.entry_list` | public |
| `/c/<slug>/numbers` | GET | `public.numbers` | public |
| `/checkout/<int:cid>/cancel` | GET | `public.checkout_cancel` | logged in |
| `/checkout/<int:cid>/demo-pay` | GET,POST | `public.demo_pay` | logged in |
| `/checkout/<int:cid>/done` | GET | `public.checkout_done` | logged in |
| `/checkout/<int:cid>/pay` | GET | `public.checkout_pay` | logged in |
| `/competitions` | GET | `public.competitions` | public |
| `/contact` | GET,POST | `public.contact` | public |
| `/deposit/<int:did>/cancel` | GET | `public.deposit_cancel` | logged in |
| `/deposit/<int:did>/demo-pay` | GET,POST | `public.deposit_demo` | logged in |
| `/deposit/<int:did>/done` | GET | `public.deposit_done` | logged in |
| `/draws` | GET | `public.draw_calendar` | public |
| `/feedback` | POST | `public.feedback` | public |
| `/forgot` | GET,POST | `public.forgot` | public |
| `/free-play/<slug>` | POST | `public.free_play` | logged in |
| `/games` | GET | `public.games` | public |
| `/healthz` | GET | `public.health` | public |
| `/healthz/deep` | GET | `public.health_deep` | public |
| `/how-it-works` | GET | `public.how_it_works` | public |
| `/instant-wins` | GET | `public.instant_wins` | public |
| `/j/select/<int:cid>` | POST | `public.journey_select` | public |
| `/legal/<slug>/v/<int:version>` | GET | `public.legal_version` | public |
| `/legal/<slug>/versions` | GET | `public.legal_versions` | public |
| `/live` | GET | `public.live` | public |
| `/login` | GET,POST | `public.login` | public |
| `/logout` | POST | `public.logout` | public |
| `/manifest.webmanifest` | GET | `public.manifest` | public |
| `/play/<slug>` | GET | `public.play` | logged in |
| `/play/<slug>/reveal-all` | POST | `public.play_reveal_all` | logged in |
| `/play/reveal/<int:tid>` | POST | `public.play_reveal` | logged in |
| `/r/<code>` | GET | `public.referral` | public |
| `/reset/<token>` | GET,POST | `public.reset` | public |
| `/results` | GET | `public.results` | public |
| `/robots.txt` | GET | `public.robots` | public |
| `/search` | GET | `public.search` | public |
| `/signup` | GET,POST | `public.signup` | public |
| `/sitemap.xml` | GET | `public.sitemap` | public |
| `/status` | GET | `public.status_page` | public |
| `/stripe/webhook` | POST | `public.stripe_webhook` | public |
| `/support` | GET,POST | `public.support_centre` | public |
| `/support/requests` | GET | `public.my_cases` | logged in |
| `/support/requests/<int:case_id>` | GET,POST | `public.case_view` | logged in |
| `/sw.js` | GET | `public.service_worker` | public |
| `/transparency` | GET | `public.transparency` | public |
| `/unsubscribe/<token>` | GET,POST | `public.unsubscribe` | public |
| `/uploads/<path:name>` | GET | `public.uploads` | public |
| `/verify/<token>` | GET | `public.verify_email` | public |
| `/watch/<slug>` | POST | `public.watch` | logged in |
| `/winners` | GET | `public.winners` | public |
| `/winners/<slug>/card.png` | GET | `public.winner_card` | public |

## Routes — Admin security (MFA, step-up) (3)

| URL | Methods | Endpoint | Protected by |
|---|---|---|---|
| `/admin/mfa/` | GET,POST | `security.verify` | staff (checked inside the view) |
| `/admin/mfa/confirm` | GET,POST | `security.confirm` | staff (checked inside the view) |
| `/admin/mfa/setup` | GET,POST | `security.setup` | staff (checked inside the view) |

## Forms (123)

| Template | Method | Posts to | Fields |
|---|---|---|---|
| `_feedback.html` | POST | `public.feedback` | comment, context, next, rating, ref |
| `_freebanner.html` | POST | `public.free_play` |  |
| `account.html` | POST | `public.resend_verification` |  |
| `account.html` | GET | `(same page)` | show, tab, tq |
| `account.html` | POST | `public.withdraw` | account_name, account_number, amount, method, paypal_email, sort_code, step |
| `account.html` | POST | `public.deposit` | amount |
| `account.html` | POST | `public.deposit_refund` |  |
| `account.html` | POST | `public.redeem` | blocks |
| `account.html` | POST | `public.set_limits` |  |
| `account.html` | POST | `public.self_exclude` | days |
| `account.html` | POST | `public.profile` | current_password, new_password, phone |
| `account.html` | POST | `public.preferences` | marketing_email, marketing_sms, reminder_emails |
| `account.html` | POST | `public.revoke_session` | sid |
| `account.html` | POST | `public.revoke_session` | sid |
| `account.html` | POST | `public.logout` |  |
| `base.html` | POST | `public.logout` |  |
| `base.html` | POST | `public.resend_verification` |  |
| `basket.html` | POST | `public.basket_update` | i, qty |
| `basket.html` | POST | `public.basket_remove` | i |
| `basket.html` | POST | `public.basket_promo` | promo, remove |
| `basket.html` | POST | `public.checkout` | idem, use_credit |
| `case_view.html` | POST | `(same page)` | body |
| `competition.html` | POST | `public.watch` | action, remind_close, remind_result |
| `competition.html` | POST | `public.free_play` |  |
| `competition.html` | POST | `public.basket_add` | answer, go, numbers, quantity, slug |
| `competitions.html` | GET | `public.competitions` | q, tab |
| `contact.html` | POST | `(same page)` | email, message, name, topic, website |
| `demo_pay.html` | POST | `(same page)` |  |
| `deposit_pay.html` | POST | `(same page)` |  |
| `entries.html` | GET | `(same page)` | q |
| `faq.html` | GET | `(same page)` | q |
| `forgot.html` | POST | `(same page)` | email |
| `login.html` | POST | `(same page)` | email, password |
| `notifications.html` | POST | `public.notifications_read` |  |
| `notifications.html` | POST | `public.notifications_read` | nid |
| `play.html` | POST | `public.play_reveal_all` |  |
| `prize.html` | POST | `(same page)` | choice |
| `prize.html` | POST | `(same page)` | address, name, phone |
| `reset.html` | POST | `(same page)` | password |
| `results.html` | GET | `(same page)` | cat, mine, month, q, year |
| `search.html` | GET | `(same page)` | q |
| `signup.html` | POST | `(same page)` | agree, dob, email, marketing, marketing_sms, name, password, phone |
| `support.html` | POST | `(same page)` | checkout_id, competition_id, message, topic, withdrawal_id |
| `unsubscribe.html` | POST | `(same page)` | reminders |
| `withdraw_review.html` | POST | `public.withdraw` | amount, step |
| `admin/analytics.html` | GET | `(same page)` | comp, device, from, to |
| `admin/announcements.html` | POST | `(same page)` | ends_at, level, link, message, starts_at |
| `admin/announcements.html` | POST | `(same page)` | end |
| `admin/approvals.html` | POST | `(same page)` | decision, id, note |
| `admin/approvals.html` | POST | `(same page)` | four_eyes |
| `admin/audit.html` | GET | `(same page)` | action, actor, from, q, target, to |
| `admin/backlog.html` | POST | `(same page)` | detail, kind, title |
| `admin/backlog_item.html` | POST | `control.backlog` | answers, bid, kind, outcome |
| `admin/backlog_item.html` | POST | `control.backlog` | bid, status |
| `admin/case.html` | POST | `(same page)` | action |
| `admin/case.html` | POST | `(same page)` | action |
| `admin/case.html` | POST | `(same page)` | action, body |
| `admin/case.html` | POST | `(same page)` | assign, checkout_id, competition_id, priority, reason, resolution, status, user_id |
| `admin/case.html` | POST | `control.backlog` | bid, detail, kind, source, source_id, title |
| `admin/centre.html` | GET | `control.search` | q |
| `admin/claim.html` | POST | `(same page)` | choice, evidence, note, status |
| `admin/communications.html` | GET | `(same page)` | q, status |
| `admin/communications.html` | POST | `control.communications_retry` |  |
| `admin/compliance.html` | POST | `(same page)` | mechanic, note, responsible |
| `admin/content_edit.html` | POST | `(same page)` | action, body, note |
| `admin/dashboard.html` | POST | `admin.publish_draft_games` |  |
| `admin/dashboard.html` | POST | `admin.random_games` | days, draft, per_price |
| `admin/dashboard.html` | POST | `admin.free_daily_game` | kind |
| `admin/dashboard.html` | POST | `admin.starter_games` | days |
| `admin/dashboard.html` | POST | `admin.delete_selected` | cid |
| `admin/edit.html` | POST | `(same page)` | auto_draw, cash_alternative, category, comp_terms, correct, description, discount_tiers, ends_at, featured, game_type, image, keep_image, kind, live_url … |
| `admin/emergency.html` | POST | `(same page)` | action |
| `admin/emergency.html` | POST | `(same page)` | action, hold_draws, reason |
| `admin/entries.html` | POST | `admin.set_status` | action |
| `admin/entries.html` | POST | `admin.set_status` | action |
| `admin/entries.html` | POST | `admin.draw` |  |
| `admin/entries.html` | POST | `admin.set_status` | action |
| `admin/entries.html` | POST | `admin.winner_story` | consent, winner_photo, winner_quote |
| `admin/entries.html` | POST | `admin.instant` | remove |
| `admin/entries.html` | POST | `admin.instant` | quantity, title, type, value |
| `admin/entries.html` | POST | `admin.instant` | prize_table |
| `admin/entries.html` | POST | `admin.instant` | random_table |
| `admin/entries.html` | POST | `admin.instant_fulfilled` |  |
| `admin/entries.html` | POST | `admin.postal` | address, answer_correct, dob, email, mode, name, phone, received |
| `admin/entries.html` | POST | `admin.redraw_comp` | confirm, reason |
| `admin/entries.html` | POST | `admin.delete_comp` | confirm |
| `admin/error_ref.html` | POST | `control.resolve_error` |  |
| `admin/experiments.html` | POST | `(same page)` | action, decision, key |
| `admin/features.html` | POST | `(same page)` | key, state |
| `admin/feedback.html` | POST | `control.backlog` | bid, source, source_id, title |
| `admin/finance.html` | GET | `(same page)` | from, to |
| `admin/flags.html` | POST | `(same page)` | run |
| `admin/flags.html` | POST | `(same page)` | fid, note, status |
| `admin/health.html` | POST | `(same page)` | run |
| `admin/mfa.html` | POST | `(same page)` | code |
| `admin/mfa.html` | POST | `security.confirm` | code, password |
| `admin/mfa.html` | POST | `(same page)` | code |
| `admin/order.html` | POST | `(same page)` | method, order_id, reason |
| `admin/payouts.html` | POST | `admin.deposit_refund_done` |  |
| `admin/payouts.html` | POST | `admin.refund_done` |  |
| `admin/payouts.html` | POST | `admin.instant_fulfilled` | back |
| `admin/payouts.html` | POST | `(same page)` | action, note, wid |
| `admin/performance.html` | GET | `(same page)` | from, to |
| `admin/postal.html` | POST | `admin.postal_process` | action, note, reason |
| `admin/prizes.html` | POST | `admin.instant_fulfilled` | back |
| `admin/promos.html` | POST | `(same page)` | category, code, comp_ids, description, expires_at, fixed, max_uses, min_spend, new_customers, per_user, percent, starts_at |
| `admin/promos.html` | POST | `(same page)` | toggle |
| `admin/reports.html` | GET | `(same page)` | from, to |
| `admin/reports.html` | POST | `(same page)` | fee_fixed_pence, fee_percent |
| `admin/reset.html` | POST | `(same page)` | accounts, competitions, confirm, promos, wallets |
| `admin/risk.html` | POST | `control.resolve_after_draw` |  |
| `admin/search.html` | GET | `(same page)` | q |
| `admin/settings.html` | POST | `(same page)` | site_status, site_status_message |
| `admin/settings.html` | POST | `(same page)` | announce, live_now_title, live_now_url |
| `admin/settings.html` | POST | `(same page)` | default_question_mode |
| `admin/targets.html` | POST | `(same page)` |  |
| `admin/targets.html` | POST | `control.backlog` | kind, source, source_id, title |
| `admin/targets.html` | POST | `control.resolve_error` |  |
| `admin/user.html` | POST | `(same page)` | action, amount, case_id, reason |
| `admin/user.html` | POST | `(same page)` | action, amount, kind, reason |
| `admin/user.html` | POST | `(same page)` | action |
| `admin/user.html` | POST | `(same page)` | action, role |
| `admin/users.html` | GET | `(same page)` | q |

## Templates (111)

`_card.html`, `_draft_notice.html`, `_feedback.html`, `_freebanner.html`, `_gamecard.html`, `_legal_contact.html`, `_progress.html`, `_stages.html`, `about.html`, `account.html`, `activity.html`, `admin/_bar.html`, `admin/analytics.html`, `admin/announcements.html`, `admin/approvals.html`, `admin/audit.html`, `admin/backlog.html`, `admin/backlog_item.html`, `admin/calendar.html`, `admin/case.html`, `admin/cases.html`, `admin/centre.html`, `admin/checkout_diagnostics.html`, `admin/claim.html`, `admin/communications.html`, `admin/compliance.html`, `admin/content.html`, `admin/content_edit.html`, `admin/dashboard.html`, `admin/edit.html`, `admin/emails.html`, `admin/emergency.html`, `admin/entries.html`, `admin/error_ref.html`, `admin/experiments.html`, `admin/features.html`, `admin/feedback.html`, `admin/finance.html`, `admin/flags.html`, `admin/health.html`, `admin/liability.html`, `admin/mfa.html`, `admin/new_pick.html`, `admin/order.html`, `admin/payouts.html`, `admin/performance.html`, `admin/postal.html`, `admin/prizes.html`, `admin/promos.html`, `admin/releases.html`, `admin/reports.html`, `admin/reset.html`, `admin/risk.html`, `admin/search.html`, `admin/segments.html`, `admin/settings.html`, `admin/stats.html`, `admin/support_insights.html`, `admin/targets.html`, `admin/timeline.html`, `admin/user.html`, `admin/users.html`, `base.html`, `basket.html`, `case_view.html`, `checkout_done.html`, `checkout_wait.html`, `comp_conditions.html`, `competition.html`, `competitions.html`, `complaints.html`, `contact.html`, `content_page.html`, `cookies.html`, `demo_pay.html`, `deposit_done.html`, `deposit_pay.html`, `draw_verify.html`, `draws.html`, `entries.html`, `error.html`, `fair.html`, `faq.html`, `forgot.html`, `free_entry.html`, `games.html`, `home.html`, `how.html`, `legal_versions.html`, `login.html`, `maintenance.html`, `my_cases.html`, `notifications.html`, `order.html`, `play.html`, `privacy.html`, `prize.html`, `reset.html`, `responsible.html`, `results.html`, `search.html`, `signup.html`, `status.html`, `support.html`, `terms.html`, `ticket.html`, `transparency.html`, `unsubscribe.html`, `winners.html`, `withdraw_review.html`, `withdrawal.html`

## Background jobs (13)

| Job | Every | What it does |
|---|---|---|
| `publish_scheduled` | 30 s | Put scheduled competitions live |
| `expire_checkouts` | 60 s | Release reservations whose 45 minutes are up |
| `close_competitions` | 30 s | Freeze final entry lists at closing time |
| `auto_draws` | 60 s | Run automatic draws |
| `settle_unrevealed` | 300 s | Pay instant-win prizes left unrevealed |
| `email_outbox` | 60 s | Send queued and retry failed emails |
| `flag_rules` | 900 s | Look for patterns worth reviewing |
| `health_alerts` | 600 s | Email staff when a health check fails |
| `watch_reminders` | 1800 s | Remind customers about saved competitions closing soon |
| `integrity` | 21600 s | Check the data for impossible conditions |
| `reconcile` | 86400 s | Match payments and refunds against Stripe |
| `reports` | 1800 s | Email the daily operations summary (7am) and weekly management report (Monday 8am) |
| `prune` | 86400 s | Tidy old job history |

## Notification kinds (16)

`cash`, `checkout`, `credit`, `deposit`, `draw`, `entry`, `free`, `game`, `order`, `payment`, `refund`, `security`, `support`, `wallet`, `win`, `withdrawal`

## Email subjects found in code (31)

- Approval needed: {KINDS[kind][0]}
- Confirm your email — {current_app.config['SITE_NAME']}
- Contact form: {topic}
- Deposit refund needs doing by hand
- Draw result: {c['title']}
- New sign-in to your account
- New sign-in to your admin account
- Please use a debit card
- Re: {k['topic']} [case #{case_id}]
- Refund for order #{cid}
- Reset your password
- We've got your message [case #{case_id}]
- We've received your withdrawal request
- Withdrawal request
- You're in! Your entries are confirmed 🎟️
- Your deposit refund
- Your free entry is in
- Your password was changed
- Your password was reset
- Your prize: {head} — {comp['title']}
- Your withdrawal has been paid 💷
- [{pr}] Support case #{case_id}: {topic}
- {c['title']} was cancelled — you've been refunded
- {current_app.config['SITE_NAME']} daily summary
- {current_app.config['SITE_NAME']} weekly report
- £{d['amount'] / 100:.2f} added to your wallet
- ⚠ After-draw check failed — competition #{comp_id}
- ⚠ New server error: {type(exc).__name__} on {endpoint}
- ⚠ {current_app.config['SITE_NAME']}: {c['name']}
- ⚠ {label}
- 🎉 You've won {c['title']}!

## CLI commands (9)

`flask backup`, `flask demo-lifecycle`, `flask dr-drill`, `flask list-admins`, `flask make-admin`, `flask release-check`, `flask remove-admin`, `flask restore-test`, `flask run-jobs`

## Feature flags (5)

- `support_centre` — Help & support centre
- `watchlist` — Save & remind me
- `deposits` — Wallet deposits
- `referrals` — Refer a friend
- `search` — Site search

## Configuration (.env) (33)

`ADMIN_IDLE_MINUTES`, `ADMIN_MFA`, `ADMIN_STEPUP_MINUTES`, `ALLOW_DEV_KEY`, `BLOCK_CREDIT_CARDS`, `COMPANY_DETAILS`, `DATA_DIR`, `DEMO_PAYMENTS`, `DISCORD_WEBHOOK_URL`, `GOODWILL_LIMIT`, `LARGE_ADJUSTMENT`, `MAIL_FROM`, `MAX_MONTHLY_LIMIT`, `POSTAL_ADDRESS`, `REFERRAL_BONUS`, `RELEASE`, `REPORT_EMAILS`, `REQUIRE_COMP_IMAGE`, `SECRET_KEY`, `SECURE_COOKIES`, `SIGNUP_RATE_LIMIT`, `SITE_NAME`, `SITE_TAGLINE`, `SITE_URL`, `SMTP_HOST`, `SMTP_PASSWORD`, `SMTP_PORT`, `SMTP_USER`, `STAGING`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `SUPPORT_EMAIL`, `TRUSTPILOT_URL`

## Third-party integrations

| Service | Used for | Code | Failure behaviour |
|---|---|---|---|
| Stripe Checkout + webhooks | Card payments, deposits, refunds, reconciliation | app/payments.py, /stripe/webhook | 3 errors in 10 min auto-pause payments; webhook retried by Stripe; reconcile job |
| SMTP (any provider) | All customer/staff email | app/mailer.py, app/notify.py | Outbox retries; failures in Customer emails log + health check; in-account copy always kept |
| Discord webhook (optional) | Draw/live announcements | DISCORD_WEBHOOK_URL | Best-effort; never blocks a draw |
| UptimeRobot / GitHub Actions (external) | Uptime checks of /healthz/deep | .github/workflows/uptime.yml | External |
| None for analytics | Cookie-free counts stored locally | app/analytics.py | n/a |

## Summary

- Routes: 158
- Forms: 123
- Templates: 111
- Background jobs: 13
- Notification kinds: 16
- CLI commands: 9
- Feature flags: 5
