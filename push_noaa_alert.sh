#!/bin/bash
curl -s -X POST "http://127.0.0.1:8080/api/noaa-alert" --data-binary "$1"
