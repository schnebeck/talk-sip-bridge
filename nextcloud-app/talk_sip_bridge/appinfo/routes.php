<?php
/*
 * talk-sip-bridge - nextcloud-app/talk_sip_bridge/appinfo/routes.php
 * The app's HTTP routes.
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

return [
	'routes' => [
		['name' => 'bridge#status', 'url' => '/status', 'verb' => 'GET'],
		['name' => 'bridge#toggle', 'url' => '/toggle', 'verb' => 'POST'],
	],
];
