<?php

declare(strict_types=1);

namespace OCA\FritzboxBridge\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\AppFramework\Http\Attribute\PublicPage;
use OCP\AppFramework\Http\DataResponse;
use OCP\IConfig;
use OCP\IRequest;
use OCP\Notification\IManager;
use Psr\Log\LoggerInterface;

/**
 * Receives ring/clear signals from the bridge daemon (Python, a separate
 * process - see bridge/talk_client.py's _notify_call_sync) when a real
 * phone call comes in on a line configured for it, and turns them into a
 * real Nextcloud notification - so a human can join the call in Talk before
 * another device (e.g. a physical phone in the same gateway-side parallel
 * ring group) answers it first.
 *
 * Authenticated by a shared secret (X-Bridge-Secret), not a Nextcloud login
 * or CSRF token: this is a machine-to-machine call from the bridge's own
 * trusted host, not a browser request.
 */
class CallSignalController extends Controller {
	public function __construct(
		string $appName,
		IRequest $request,
		private IConfig $config,
		private IManager $notificationManager,
		private LoggerInterface $logger,
	) {
		parent::__construct($appName, $request);
	}

	private function checkSecret(): ?DataResponse {
		$configured = $this->config->getAppValue('fritzboxbridge', 'notify_secret', '');
		$given = $this->request->getHeader('X-Bridge-Secret');
		if ($configured === '' || !hash_equals($configured, $given)) {
			return new DataResponse(['error' => 'unauthorized'], 403);
		}
		return null;
	}

	/**
	 * @PublicPage
	 * @NoCSRFRequired
	 */
	#[PublicPage]
	#[NoCSRFRequired]
	public function ring(string $callId, string $caller = '', string $roomToken = '', string $user = ''): DataResponse {
		if ($deny = $this->checkSecret()) {
			return $deny;
		}
		if ($user === '' || $roomToken === '') {
			return new DataResponse(['error' => 'user and roomToken are required'], 400);
		}
		$notification = $this->notificationManager->createNotification();
		$notification->setApp('fritzboxbridge')
			->setUser($user)
			->setDateTime(new \DateTime())
			->setObject('call', $callId)
			->setSubject('incoming_call', ['caller' => $caller, 'roomToken' => $roomToken]);
		try {
			$this->notificationManager->notify($notification);
		} catch (\InvalidArgumentException $e) {
			$this->logger->warning('Could not create incoming-call notification: ' . $e->getMessage(), ['app' => 'fritzboxbridge']);
			return new DataResponse(['error' => $e->getMessage()], 400);
		}
		return new DataResponse(['ok' => true]);
	}

	/**
	 * @PublicPage
	 * @NoCSRFRequired
	 */
	#[PublicPage]
	#[NoCSRFRequired]
	public function clear(string $callId, string $user = ''): DataResponse {
		if ($deny = $this->checkSecret()) {
			return $deny;
		}
		$notification = $this->notificationManager->createNotification();
		$notification->setApp('fritzboxbridge')->setObject('call', $callId);
		if ($user !== '') {
			$notification->setUser($user);
		}
		$this->notificationManager->markProcessed($notification);
		return new DataResponse(['ok' => true]);
	}
}
