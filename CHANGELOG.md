# Changelog

## [0.9.0](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.8.2...v0.9.0) (2026-10-09)


### Features

* stream GpsHeading as a Heading sensor and a heading attribute on Location ([#15](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/15)) ([14ef883](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/14ef883b090c6a32bdbcb17bb40e6bb5c84c1dfd))

## [0.8.2](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.8.1...v0.8.2) (2026-09-30)


### Bug Fixes

* request vehicle_location scope and proxy nginx root path ([#13](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/13)) ([f401214](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/f40121440147eede4780b94c53b44827291af87e))

## [0.8.1](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.8.0...v0.8.1) (2026-09-28)


### Bug Fixes

* stream the navigation destination at 1 s instead of 30 s ([#9](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/9)) ([9b35ad6](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/9b35ad6e2f36654e7f665511153bf5e612361b88))

## [0.8.0](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.7.1...v0.8.0) (2026-09-26)


### Features

* add an opt-in navigate service that sends a destination to the car ([#7](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/7)) ([a361a9e](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/a361a9e2819123c0edb14ad3162cb8b7680c566c))

## [0.7.1](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.7.0...v0.7.1) (2026-09-24)


### Bug Fixes

* ship a brand icon so HA and HACS show one ([c1ad735](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/c1ad735175a38ab4f84a80cc6fc381008287ff81))

## [0.7.0](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.6.2...v0.7.0) (2026-09-24)


### Features

* add EU region support in the config flow ([#2](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/2)) ([d4ec372](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/d4ec372af6e8d82f62fea887aeb6fecd9895dbf1))


### Bug Fixes

* add Let's Encrypt Gen-Y roots (YE/YR) to the default CA bundle ([f33ad2b](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/f33ad2b2c20aabbdcb1bf8c8ab9b3143e4df5c44))
