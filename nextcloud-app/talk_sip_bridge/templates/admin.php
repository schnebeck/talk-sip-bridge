<?php
/*
 * talk-sip-bridge - nextcloud-app/talk_sip_bridge/templates/admin.php
 * The markup of the admin settings panel.
 *
 *   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
 *   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
 *   Written by Anthropic Claude Opus 5 - AI generated content.
 *
 *   Free software under the GNU Affero General Public License, version 3 or
 *   later. There is no warranty, to the extent permitted by law. The full
 *   text is in LICENSES/AGPL-3.0-or-later.txt.
 *
 * SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
 * SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

/** @var \OCP\IL10N $l */
\OCP\Util::addScript('talk_sip_bridge', 'admin-settings');
\OCP\Util::addStyle('talk_sip_bridge', 'admin-settings');
?>
<div id="talk_sip_bridge-admin" class="section">
	<h2><?php p($l->t('Talk SIP Bridge')); ?></h2>
	<p class="settings-hint">
		<?php p($l->t('Connects a SIP phone line to Talk calls. Incoming calls appear as regular participants; outgoing calls use Talk\'s own "call a phone number".')); ?>
	</p>

	<div id="talk_sip_bridge-status-block">
		<p>
			<strong><?php p($l->t('Status:')); ?></strong>
			<span id="talk_sip_bridge-status-text"><?php p($l->t('Loading …')); ?></span>
		</p>
		<button id="talk_sip_bridge-toggle-btn" class="button" disabled>
			<?php p($l->t('…')); ?>
		</button>
	</div>
</div>
