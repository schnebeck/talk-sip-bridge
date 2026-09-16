<?php

declare(strict_types=1);

namespace OCA\FritzboxBridge\Notification;

use OCP\IURLGenerator;
use OCP\L10N\IFactory;
use OCP\Notification\INotification;
use OCP\Notification\INotifier;
use OCP\Notification\UnknownNotificationException;

/**
 * Turns a bare "incoming_call" notification (created by
 * CallSignalController::ring, triggered from the bridge daemon) into
 * readable text and a link straight into the call's Talk room.
 */
class Notifier implements INotifier {
	public function __construct(
		private IFactory $l10nFactory,
		private IURLGenerator $urlGenerator,
	) {
	}

	public function getID(): string {
		return 'fritzboxbridge';
	}

	public function getName(): string {
		return $this->l10nFactory->get('fritzboxbridge')->t('FritzBox Talk Bridge');
	}

	public function prepare(INotification $notification, string $languageCode): INotification {
		if ($notification->getApp() !== 'fritzboxbridge') {
			throw new UnknownNotificationException();
		}
		if ($notification->getSubject() !== 'incoming_call') {
			throw new UnknownNotificationException();
		}

		$l = $this->l10nFactory->get('fritzboxbridge', $languageCode);
		$params = $notification->getSubjectParameters();
		$caller = $params['caller'] ?? '';
		$roomToken = $params['roomToken'] ?? '';

		$notification->setParsedSubject(
			$caller !== ''
				? $l->t('Incoming call from %s', [$caller])
				: $l->t('Incoming call')
		);
		if ($roomToken !== '') {
			$notification->setLink($this->urlGenerator->getAbsoluteURL('/call/' . $roomToken));
		}
		return $notification;
	}
}
