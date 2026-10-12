# Multi-view region marking — benchmark (tools/mv_benchmark.py, seed 7)

Truth: 67.2 m³ inside the r = 5.6 m ring (98.5 m²). See MULTIVIEW_REGION_PROGRESS.md §5.

## Full matrix (4 presets run in parallel, 3 BLAS threads each — runtimes contended; one `twosided_b` row's 12956 s is a machine suspend, not compute)

| preset | scenario | groups | used | 4×2 | prec | recall | bg incl | pt loss | bnd err m | unc frac | min IoU | status | vol err % | cut lo | cut | cut up | bounds∋truth | ±err∋truth | select s | measure s | peak RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| twosided_cal | legacy 1-view (ray-cast) | — | — | — | 0.998 | 0.996 | 0.002 | 0.004 | 0.018 | — | — | ok | 1.476 | — | 66.2 | — | — | yes | 15.9 | 131.9 | 531.4 |
| twosided_cal | clean x1 | 1 | 1 | no | 0.992 | 0.990 | 0.008 | 0.010 | 0.049 | 0.075 | — | indicative | 1.418 | 66.1 | 66.2 | 66.3 | no | yes | 7.179 | 131.3 | 828.8 |
| | ↳ reasons: MV_insufficient_pov; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_cal | clean x2 | 2 | 2 | no | 0.994 | 0.992 | 0.006 | 0.008 | 0.040 | 0.554 | 0.915 | indicative | 1.403 | 20.4 | 66.2 | 66.3 | no | yes | 10.1 | 130.5 | 857.4 |
| | ↳ reasons: MV_insufficient_pov, MV_poor_visibility, MV_uncertain_boundary; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_cal | clean x4 | 4 | 4 | no | 0.995 | 0.996 | 0.005 | 0.004 | 0.030 | 0.063 | 0.944 | indicative | 1.383 | 66.1 | 66.2 | 66.3 | no | yes | 8.802 | 114.2 | 857.9 |
| | ↳ reasons: MV_insufficient_pov; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_cal | clean x8 | 4 | 8 | yes | 0.996 | 0.995 | 0.004 | 0.005 | 0.027 | 0.062 | 0.911 | ok | 1.391 | 66.1 | 66.2 | 66.3 | no | yes | 11.4 | 117.9 | 873.4 |
| twosided_cal | clean x16 | 12 | 16 | yes | 0.995 | 0.995 | 0.005 | 0.005 | 0.031 | 0.060 | 0.908 | ok | 1.394 | 66.1 | 66.2 | 66.3 | no | yes | 21.7 | 124.7 | 881.3 |
| twosided_cal | boundary noise σ=3px x8 | 4 | 8 | yes | 0.995 | 0.994 | 0.005 | 0.006 | 0.037 | 0.075 | 0.892 | ok | 1.381 | 66.1 | 66.2 | 66.3 | no | yes | 12.2 | 127.5 | 881.7 |
| twosided_cal | boundary noise σ=8px x8 | 4 | 8 | yes | 0.977 | 0.995 | 0.023 | 0.005 | 0.089 | 0.136 | 0.840 | ok | 1.223 | 66.2 | 66.3 | 66.5 | no | yes | 15.2 | 126.7 | 882.5 |
| twosided_cal | one bad mask x8 | 4 | 7 | no | 0.996 | 0.996 | 0.004 | 0.004 | 0.027 | 0.063 | 0.177 | indicative | 1.390 | 66.1 | 66.2 | 66.3 | no | yes | 12.6 | 118.6 | 883.4 |
| | ↳ reasons: MV_insufficient_pov, MV_mask_disagreement; rejected: IMG_00.png | | | | | | | | | | | | | | | | | | | | |
| twosided_cal | one bad mask x9 (8 good + 1) | 4 | 8 | yes | 0.996 | 0.995 | 0.004 | 0.005 | 0.027 | 0.062 | 0.125 | indicative | 1.391 | 66.1 | 66.2 | 66.3 | no | yes | 13.3 | 113.8 | 883.4 |
| | ↳ reasons: MV_mask_disagreement; rejected: IMG_08.png | | | | | | | | | | | | | | | | | | | | |
| twosided_cal | occluder in front of 2 groups x8 | 4 | 8 | yes | 0.997 | 0.993 | 0.003 | 0.007 | 0.030 | 0.065 | 0.911 | — | — | — | — | — | — | — | 12.5 | — | 909.6 |
| twosided_cal | missing POV groups (2 groups) x8 | 2 | 8 | no | 0.993 | 0.991 | 0.007 | 0.009 | 0.045 | 0.072 | 0.992 | indicative | 1.395 | 66.1 | 66.2 | 66.3 | no | yes | 11.4 | 104.9 | 909.6 |
| | ↳ reasons: MV_insufficient_pov; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | legacy 1-view (ray-cast) | — | — | — | 0.997 | 0.997 | 0.003 | 0.003 | 0.019 | — | — | indicative | 16.8 | — | 78.5 | — | — | no | 15.6 | 202.5 | 989.8 |
| | ↳ reasons: G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | clean x1 | 1 | 1 | no | 0.991 | 0.996 | 0.009 | 0.004 | 0.047 | 0.072 | — | indicative | 17.5 | 78.6 | 78.9 | 79.1 | no | no | 6.293 | 332.8 | 1039.4 |
| | ↳ reasons: G9_datum_unstable, MV_insufficient_pov; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | clean x2 | 2 | 2 | no | 0.995 | 0.994 | 0.005 | 0.006 | 0.038 | 0.573 | 0.916 | indicative | 17.2 | 24.6 | 78.7 | 78.9 | yes | yes | 6.580 | 279.3 | 1048.1 |
| | ↳ reasons: G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility, MV_uncertain_boundary; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | clean x4 | 4 | 4 | no | 0.996 | 0.995 | 0.004 | 0.005 | 0.035 | 0.064 | 0.946 | indicative | 17.4 | 78.6 | 78.8 | 79.1 | no | no | 8.268 | 324.6 | 1050.9 |
| | ↳ reasons: G9_datum_unstable, MV_insufficient_pov; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | clean x8 | 4 | 8 | yes | 0.996 | 0.996 | 0.004 | 0.004 | 0.033 | 0.065 | 0.911 | indicative | 17.4 | 78.6 | 78.8 | 79.1 | no | no | 13.4 | 296.7 | 1057.0 |
| | ↳ reasons: G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | clean x16 | 12 | 16 | yes | 0.996 | 0.995 | 0.004 | 0.005 | 0.034 | 0.065 | 0.908 | indicative | 17.4 | 78.6 | 78.8 | 79.1 | no | no | 19.1 | 267.6 | 1057.0 |
| | ↳ reasons: G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | boundary noise σ=3px x8 | 4 | 8 | yes | 0.993 | 0.995 | 0.007 | 0.005 | 0.043 | 0.073 | 0.890 | indicative | 17.4 | 78.6 | 78.8 | 79.1 | no | no | 10.6 | 12955.9 | 1057.0 |
| | ↳ reasons: G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | boundary noise σ=8px x8 | 4 | 8 | yes | 0.980 | 0.994 | 0.020 | 0.006 | 0.088 | 0.138 | 0.838 | indicative | 18.1 | 78.7 | 79.3 | 79.8 | no | no | 11.6 | 261.1 | 1057.0 |
| | ↳ reasons: G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | |
| twosided_b | one bad mask x8 | 4 | 7 | no | 0.996 | 0.995 | 0.004 | 0.005 | 0.034 | 0.065 | 0.182 | indicative | 17.4 | 78.6 | 78.8 | 79.1 | no | no | 11.5 | 248.7 | 1057.0 |
| | ↳ reasons: G9_datum_unstable, MV_insufficient_pov, MV_mask_disagreement; rejected: IMG_00.png | | | | | | | | | | | | | | | | | | | | |
| twosided_b | one bad mask x9 (8 good + 1) | 4 | 8 | yes | 0.996 | 0.996 | 0.004 | 0.004 | 0.033 | 0.065 | 0.139 | indicative | 17.4 | 78.6 | 78.8 | 79.1 | no | no | 11.7 | 238.1 | 1057.0 |
| | ↳ reasons: G9_datum_unstable, MV_mask_disagreement; rejected: IMG_08.png | | | | | | | | | | | | | | | | | | | | |
| twosided_b | occluder in front of 2 groups x8 | 4 | 8 | yes | 0.997 | 0.996 | 0.003 | 0.004 | 0.029 | 0.060 | 0.911 | — | — | — | — | — | — | — | 12.1 | — | 1057.0 |
| twosided_b | missing POV groups (2 groups) x8 | 2 | 8 | no | 0.991 | 0.993 | 0.009 | 0.007 | 0.052 | 0.071 | 0.992 | indicative | 17.6 | 78.7 | 79.0 | 79.2 | no | no | 8.156 | 234.1 | 1058.2 |
| | ↳ reasons: G9_datum_unstable, MV_insufficient_pov; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | legacy 1-view (ray-cast) | — | — | — | 0.996 | 0.988 | 0.004 | 0.012 | 0.064 | — | — | rejected | 26.0 | — | 49.7 | — | — | yes | 6.056 | 73.3 | 380.5 |
| | ↳ reasons: G6_rejected, G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | clean x1 | 1 | 1 | no | 0.992 | 0.989 | 0.008 | 0.011 | 0.095 | 0.046 | — | indicative | 27.8 | 48.4 | 48.5 | 48.5 | no | yes | 4.071 | 553.8 | 960.3 |
| | ↳ reasons: G6_indicative, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | clean x2 | 2 | 2 | no | 0.995 | 0.989 | 0.005 | 0.011 | 0.128 | 0.098 | 0.930 | rejected | 26.2 | 49.5 | 49.5 | 49.6 | no | yes | 3.169 | 58.7 | 960.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | clean x4 | 4 | 4 | no | 0.995 | 0.988 | 0.005 | 0.012 | 0.112 | 0.065 | 0.938 | rejected | 26.1 | 49.6 | 49.6 | 49.7 | no | yes | 3.979 | 59.3 | 960.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | clean x8 | 4 | 8 | yes | 0.995 | 0.988 | 0.005 | 0.012 | 0.114 | 0.067 | 0.936 | rejected | 26.0 | 49.6 | 49.7 | 49.7 | no | yes | 5.274 | 63.9 | 960.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | clean x16 | 8 | 16 | yes | 0.995 | 0.989 | 0.005 | 0.011 | 0.095 | 0.075 | 0.931 | rejected | 26.1 | 49.5 | 49.6 | 49.7 | no | yes | 7.456 | 59.7 | 960.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | boundary noise σ=3px x8 | 4 | 8 | yes | 0.995 | 0.988 | 0.005 | 0.012 | 0.103 | 0.080 | 0.917 | rejected | 26.1 | 49.5 | 49.6 | 49.7 | no | yes | 6.986 | 70.4 | 960.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | boundary noise σ=8px x8 | 4 | 8 | yes | 0.989 | 0.992 | 0.011 | 0.008 | 0.114 | 0.120 | 0.852 | rejected | 24.6 | 50.5 | 50.6 | 50.7 | no | yes | 7.568 | 66.8 | 960.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| arc | one bad mask x8 | 4 | 7 | no | 0.995 | 0.988 | 0.005 | 0.012 | 0.114 | 0.059 | 0.152 | rejected | 26.1 | 49.6 | 49.7 | 49.7 | no | yes | 5.993 | 59.4 | 960.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_mask_disagreement, MV_poor_visibility; rejected: IMG_10.png | | | | | | | | | | | | | | | | | | | | |
| arc | one bad mask x9 (8 good + 1) | 4 | 8 | yes | 0.995 | 0.988 | 0.005 | 0.012 | 0.114 | 0.067 | 0.146 | rejected | 26.0 | 49.6 | 49.7 | 49.7 | no | yes | 5.724 | 56.8 | 960.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_mask_disagreement, MV_poor_visibility; rejected: IMG_07.png | | | | | | | | | | | | | | | | | | | | |
| arc | occluder in front of 2 groups x8 | 4 | 8 | yes | 1.000 | 0.965 | 0.000 | 0.035 | 1.154 | 0.671 | 0.894 | — | — | — | — | — | — | — | 8.969 | — | 960.3 |
| arc | missing POV groups (2 groups) x8 | 2 | 8 | no | 0.993 | 0.989 | 0.007 | 0.011 | 0.085 | 0.049 | 0.994 | indicative | 26.1 | 49.6 | 49.6 | 49.7 | no | yes | 5.145 | 63.7 | 960.3 |
| | ↳ reasons: G6_indicative, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| sparse8 | legacy 1-view (ray-cast) | — | — | — | 0.999 | 0.978 | 0.001 | 0.022 | 0.119 | — | — | rejected | 39.1 | — | 40.9 | — | — | yes | 3.191 | 54.0 | 323.0 |
| | ↳ reasons: G5_partial, G6_rejected, G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | |
| sparse8 | clean x1 | 1 | 1 | no | 0.995 | 0.978 | 0.005 | 0.022 | 0.151 | 0.039 | — | rejected | 32.5 | 45.2 | 45.3 | 45.5 | no | yes | 4.052 | 46.1 | 364.9 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| sparse8 | clean x2 | 2 | 2 | no | 0.997 | 0.977 | 0.003 | 0.023 | 0.161 | 0.070 | 0.980 | rejected | 32.5 | 45.2 | 45.3 | 45.5 | no | yes | 3.422 | 41.3 | 366.7 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| sparse8 | clean x4 | 3 | 4 | no | 0.997 | 0.978 | 0.003 | 0.022 | 0.145 | 0.073 | 0.981 | rejected | 32.6 | 45.2 | 45.3 | 45.4 | no | yes | 4.782 | 39.3 | 366.7 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| sparse8 | clean x8 | 5 | 8 | no | 0.996 | 0.978 | 0.004 | 0.022 | 0.142 | 0.062 | 0.967 | rejected | 32.7 | 45.1 | 45.2 | 45.4 | no | yes | 6.525 | 42.6 | 366.7 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| sparse8 | boundary noise σ=3px x8 | 5 | 8 | no | 0.997 | 0.978 | 0.003 | 0.022 | 0.144 | 0.073 | 0.950 | rejected | 32.6 | 45.1 | 45.3 | 45.5 | no | yes | 5.780 | 38.9 | 367.0 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| sparse8 | boundary noise σ=8px x8 | 5 | 8 | no | 0.993 | 0.982 | 0.007 | 0.018 | 0.144 | 0.121 | 0.913 | rejected | 31.8 | 45.5 | 45.8 | 46.1 | no | yes | 6.657 | 44.3 | 368.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |
| sparse8 | one bad mask x8 | 5 | 7 | no | 0.996 | 0.979 | 0.004 | 0.021 | 0.142 | 0.062 | 0.157 | rejected | 32.6 | 45.1 | 45.3 | 45.4 | no | yes | 6.081 | 45.3 | 368.3 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_mask_disagreement, MV_poor_visibility; rejected: IMG_04.png | | | | | | | | | | | | | | | | | | | | |
| sparse8 | occluder in front of 2 groups x8 | 5 | 8 | no | 1.000 | 0.862 | 0.000 | 0.138 | 1.802 | 0.978 | 0.847 | — | — | — | — | — | — | — | 8.111 | — | 388.1 |
| sparse8 | missing POV groups (2 groups) x4 | 2 | 4 | no | 0.997 | 0.977 | 0.003 | 0.023 | 0.166 | 0.052 | 0.973 | rejected | 32.6 | 45.2 | 45.3 | 45.4 | no | yes | 4.909 | 38.9 | 388.1 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | |

## Key scenarios, run alone (uncontended runtimes; adds the pipeline's own measured..upper range)

| preset | scenario | groups | used | 4×2 | prec | recall | bg incl | pt loss | bnd err m | unc frac | min IoU | status | vol err % | cut lo | cut | cut up | region bounds∋truth | G6 upper | range∋truth | ±err∋truth | select s | measure s | peak RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| arc | legacy 1-view (ray-cast) | — | — | — | 0.996 | 0.988 | 0.004 | 0.012 | 0.064 | — | — | rejected | 26.0 | — | 49.7 | — | — | 144.1 | yes | yes | 3.664 | 46.2 | 382.5 |
| | ↳ reasons: G6_rejected, G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| arc | clean x1 | 1 | 1 | no | 0.992 | 0.989 | 0.008 | 0.011 | 0.095 | 0.046 | — | indicative | 27.8 | 48.4 | 48.5 | 48.5 | no | 139.5 | yes | yes | 3.151 | 407.4 | 969.0 |
| | ↳ reasons: G6_indicative, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| arc | clean x8 | 4 | 8 | yes | 0.995 | 0.988 | 0.005 | 0.012 | 0.114 | 0.067 | 0.936 | rejected | 26.0 | 49.6 | 49.7 | 49.7 | no | 145.3 | yes | yes | 5.521 | 51.4 | 969.0 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| arc | clean x16 | 8 | 16 | yes | 0.995 | 0.989 | 0.005 | 0.011 | 0.095 | 0.075 | 0.931 | rejected | 26.1 | 49.5 | 49.6 | 49.7 | no | 144.2 | yes | yes | 7.231 | 52.5 | 969.0 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| twosided_cal | legacy 1-view (ray-cast) | — | — | — | 0.998 | 0.996 | 0.002 | 0.004 | 0.018 | — | — | ok | 1.476 | — | 66.2 | — | — | 70.6 | yes | yes | 11.1 | 89.7 | 969.0 |
| twosided_cal | clean x1 | 1 | 1 | no | 0.992 | 0.990 | 0.008 | 0.010 | 0.049 | 0.075 | — | indicative | 1.418 | 66.1 | 66.2 | 66.3 | no | 70.7 | yes | yes | 6.253 | 98.9 | 969.0 |
| | ↳ reasons: MV_insufficient_pov; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| twosided_cal | clean x8 | 4 | 8 | yes | 0.996 | 0.995 | 0.004 | 0.005 | 0.027 | 0.062 | 0.911 | ok | 1.391 | 66.1 | 66.2 | 66.3 | no | 70.7 | yes | yes | 10.3 | 97.3 | 969.0 |
| twosided_cal | clean x16 | 12 | 16 | yes | 0.995 | 0.995 | 0.005 | 0.005 | 0.031 | 0.060 | 0.908 | ok | 1.394 | 66.1 | 66.2 | 66.3 | no | 70.7 | yes | yes | 16.9 | 119.8 | 969.0 |
| twosided_b | legacy 1-view (ray-cast) | — | — | — | 0.997 | 0.997 | 0.003 | 0.003 | 0.019 | — | — | indicative | 16.8 | — | 78.5 | — | — | 82.8 | no | no | 12.3 | 167.5 | 1043.4 |
| | ↳ reasons: G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| twosided_b | clean x1 | 1 | 1 | no | 0.991 | 0.996 | 0.009 | 0.004 | 0.047 | 0.072 | — | indicative | 17.5 | 78.6 | 78.9 | 79.1 | no | 83.3 | no | no | 6.451 | 265.6 | 1068.1 |
| | ↳ reasons: G9_datum_unstable, MV_insufficient_pov; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| twosided_b | clean x8 | 4 | 8 | yes | 0.996 | 0.996 | 0.004 | 0.004 | 0.033 | 0.065 | 0.911 | indicative | 17.4 | 78.6 | 78.8 | 79.1 | no | 83.2 | no | no | 10.5 | 241.1 | 1077.8 |
| | ↳ reasons: G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| twosided_b | clean x16 | 12 | 16 | yes | 0.996 | 0.995 | 0.004 | 0.005 | 0.034 | 0.065 | 0.908 | indicative | 17.4 | 78.6 | 78.8 | 79.1 | no | 83.3 | no | no | 15.7 | 246.9 | 1080.5 |
| | ↳ reasons: G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| sparse8 | legacy 1-view (ray-cast) | — | — | — | 0.999 | 0.978 | 0.001 | 0.022 | 0.119 | — | — | rejected | 39.1 | — | 40.9 | — | — | 163.7 | yes | yes | 1.003 | 19.2 | 1080.5 |
| | ↳ reasons: G5_partial, G6_rejected, G9_datum_unstable; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| sparse8 | clean x1 | 1 | 1 | no | 0.995 | 0.978 | 0.005 | 0.022 | 0.151 | 0.039 | — | rejected | 32.5 | 45.2 | 45.3 | 45.5 | no | 169.6 | yes | yes | 1.509 | 20.2 | 1080.5 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
| sparse8 | clean x8 | 5 | 8 | no | 0.996 | 0.978 | 0.004 | 0.022 | 0.142 | 0.062 | 0.967 | rejected | 32.7 | 45.1 | 45.2 | 45.4 | no | 170.0 | yes | yes | 3.221 | 21.7 | 1080.5 |
| | ↳ reasons: G6_rejected, G9_datum_unstable, MV_insufficient_pov, MV_poor_visibility; rejected: — | | | | | | | | | | | | | | | | | | | | | | |
