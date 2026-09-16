<?php
return [
    'routes' => [
        ['name' => 'bridge#status', 'url' => '/status', 'verb' => 'GET'],
        ['name' => 'bridge#toggle', 'url' => '/toggle', 'verb' => 'POST'],
    ],
];
