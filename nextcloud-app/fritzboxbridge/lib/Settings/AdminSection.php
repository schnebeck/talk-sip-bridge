<?php

declare(strict_types=1);

namespace OCA\FritzboxBridge\Settings;

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
		return 'fritzboxbridge';
	}

	public function getName(): string {
		return $this->l->t('FritzBox Talk Bridge');
	}

	public function getPriority(): int {
		return 80;
	}

	public function getIcon(): string {
		return $this->urlGenerator->imagePath('core', 'actions/phone.svg');
	}
}
