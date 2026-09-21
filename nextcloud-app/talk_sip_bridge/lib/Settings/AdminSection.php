<?php
/*
 * talk-sip-bridge - nextcloud-app/talk_sip_bridge/lib/Settings/AdminSection.php
 * The app's own section in Nextcloud's admin settings.
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

declare(strict_types=1);

namespace OCA\TalkSipBridge\Settings;

use OCP\IL10N;
use OCP\IURLGenerator;
use OCP\Settings\IIconSection;

class AdminSection implements IIconSection {
	public function __construct(
		private IL10N $l,
		private IURLGenerator $urlGenerator,
	) {
	}

	public function getID(): string {
		return 'talk_sip_bridge';
	}

	public function getName(): string {
		return $this->l->t('Talk SIP Bridge');
	}

	public function getPriority(): int {
		return 80;
	}

	public function getIcon(): string {
		return $this->urlGenerator->imagePath('core', 'actions/phone.svg');
	}
}
