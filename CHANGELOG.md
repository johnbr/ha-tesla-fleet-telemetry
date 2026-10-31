# Changelog

## [0.11.4](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.11.3...v0.11.4) (2026-10-31)


### Bug Fixes

* renew a stamped config the car no longer reports ([#44](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/44)) ([4ff5a00](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/4ff5a00842a6c52e1a96ad6c3981125f755634ff))

## [0.11.3](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.11.2...v0.11.3) (2026-10-31)


### Bug Fixes

* renew the telemetry config when the car's copy is about to expire ([#41](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/41)) ([a577d3d](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/a577d3da934da87801ee1766c1e420143f048345))

## [0.11.2](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.11.1...v0.11.2) (2026-10-31)


### Bug Fixes

* renew an inherited telemetry config instead of skipping forever ([#36](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/36)) ([4efa97d](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/4efa97dcf01fc6c4a9d0eb9c3958e81fedc04210))

## [0.11.1](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.11.0...v0.11.1) (2026-10-30)


### Bug Fixes

* send the minimum_delta Tesla requires for SelfDrivingMilesSinceReset ([#34](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/34)) ([f05aa13](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/f05aa13b71bd34546120bf033f8cf82d44b65fdd))

## [0.11.0](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.10.3...v0.11.0) (2026-10-30)


### Features

* add a Self-driving distance sensor (SelfDrivingMilesSinceReset) ([#32](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/32)) ([0e6d048](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/0e6d04869b8be867e487bff128796841a202720e))

## [0.10.3](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.10.2...v0.10.3) (2026-10-21)


### Bug Fixes

* ship the upstream Apache-2.0 license with the vendored proto files ([#28](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/28)) ([305db4b](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/305db4b1145838db34f20e31d7b0f495bce14041))

## [0.10.2](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.10.1...v0.10.2) (2026-10-20)


### Bug Fixes

* harden the telemetry endpoint and admin services ahead of HACS review ([#26](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/26)) ([6ebf041](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/6ebf0418bd6718ffae046a9c731f89e4d53ff8eb))

## [0.10.1](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.10.0...v0.10.1) (2026-10-14)


### Bug Fixes

* log an unrecognised VIN with %r so it can't forge a log line ([#21](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/21)) ([5695ebb](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/5695ebb8862e2ca13fd60ff65784b8635245619b))

## [0.10.0](https://github.com/johnbr/ha-tesla-fleet-telemetry/compare/v0.9.0...v0.10.0) (2026-10-09)


### Features

* give catalog-added signals a generic sensor, added and removed live ([#17](https://github.com/johnbr/ha-tesla-fleet-telemetry/issues/17)) ([57c675c](https://github.com/johnbr/ha-tesla-fleet-telemetry/commit/57c675c8cbcdb7b62f6d03923555bcf52db1c619))

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
