<?php
/*
 * talk-sip-bridge - nextcloud-app/talk_sip_bridge/lib/Controller/BridgeController.php
 * Passes the admin page's requests on to the daemon's control API.
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

namespace OCA\TalkSipBridge\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\DataResponse;
use OCP\Http\Client\IClientService;
use OCP\IConfig;
use OCP\IRequest;
use Psr\Log\LoggerInterface;

/**
 * Pure proxy layer to the local bridge daemon (Python, a separate process -
 * see bridge/daemon.py). Contains no SIP/signaling logic itself.
 */
class BridgeController extends Controller {
	public function __construct(
		string $appName,
		IRequest $request,
		private IConfig $config,
		private IClientService $clientService,
		private LoggerInterface $logger,
	) {
		parent::__construct($appName, $request);
	}

	private function bridgeUrl(): string {
		return rtrim($this->config->getAppValue('talk_sip_bridge', 'bridge_url', 'http://127.0.0.1:8765'), '/');
	}

	private function callBridge(string $path, string $method = 'GET'): DataResponse {
		$client = $this->clientService->newClient();
		// The bridge daemon is only ever reached at a fixed, admin-configured
		// local address (e.g. the Docker bridge gateway) - not user input -
		// so explicitly allowing a local address here is not an SSRF risk.
		$options = ['timeout' => 5, 'nextcloud' => ['allow_local_address' => true]];
		try {
			$response = $method === 'POST'
				? $client->post($this->bridgeUrl() . $path, $options)
				: $client->get($this->bridgeUrl() . $path, $options);
			$decoded = json_decode($response->getBody(), true);
			return new DataResponse($decoded ?? ['raw' => $response->getBody()], $response->getStatusCode());
		} catch (\Exception $e) {
			$this->logger->warning('Talk SIP Bridge unreachable: ' . $e->getMessage(), ['app' => 'talk_sip_bridge']);
			return new DataResponse(['error' => $e->getMessage()], 502);
		}
	}

	public function status(): DataResponse {
		return $this->callBridge('/status');
	}

	public function toggle(): DataResponse {
		return $this->callBridge('/toggle', 'POST');
	}
}
