<?php
return [
    'routes' => [
        ['name' => 'bridge#status', 'url' => '/status', 'verb' => 'GET'],
        ['name' => 'bridge#toggle', 'url' => '/toggle', 'verb' => 'POST'],
        ['name' => 'call_signal#ring', 'url' => '/call/ring', 'verb' => 'POST'],
        ['name' => 'call_signal#clear', 'url' => '/call/clear', 'verb' => 'POST'],
    ],
];
