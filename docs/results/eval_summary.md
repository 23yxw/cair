# Evaluation summary

| setting | n | exact | compatible | wrong | top3 hit | unsupported claims | primary study cov. | mean tool calls | mean images | mean tokens | mean s | degraded | leak-free |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S1 | 10 | 5 | 0 | 5 | 7 | 0 | 1.00 | 0.0 | 7.2 | 14327 | 160 | 0 | 10/10 |
| S2 | 10 | 6 | 0 | 4 | 6 | 0 | 1.00 | 11.0 | 8.3 | 77408 | 85 | 0 | 10/10 |
| S3 | 10 | 6 | 0 | 4 | 7 | 0 | 1.00 | 10.3 | 6.6 | 81426 | 135 | 0 | 10/10 |
| S4 | 10 | 7 | 1 | 2 | 9 | 0 | 1.00 | 11.6 | 5.4 | 75285 | 134 | 0 | 10/10 |
| S5 | 10 | 6 | 1 | 3 | 6 | 0 | 1.00 | 11.0 | 9.7 | 90418 | 141 | 0 | 10/10 |

## S1

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Acute appendicitis with periappendiceal abscess | wrong | n | 0 | 6 | 12796 | single_pass |  |
| case-02 | breast fibromatosis | Desmoid-type fibromatosis | exact | y | 0 | 7 | 15783 | single_pass |  |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Bone metastasis from bladder cancer | wrong | n | 0 | 4 | 12481 | single_pass |  |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic diverticulum | exact | y | 0 | 2 | 9802 | single_pass |  |
| case-05 | extensive multi vessels coronary artery disease | Giant Right Coronary Artery Aneurysm | wrong | y | 0 | 13 | 13819 | single_pass |  |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 0 | 5 | 8310 | single_pass |  |
| case-07 | patellar tendon rupture | Anterior Cruciate Ligament (ACL) tear | wrong | y | 0 | 7 | 11000 | single_pass |  |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Congenital Adrenal Hyperplasia (CAH) with ambiguous genitalia and hydrometrocolpos | wrong | n | 0 | 16 | 20775 | single_pass |  |
| case-09 | slipped capital femoral epiphysis scfe | Slipped Capital Femoral Epiphysis (SCFE) | exact | y | 0 | 4 | 17119 | single_pass |  |
| case-10 | spondylolisthesis with follow up | Dysplastic spondylolisthesis secondary to hemisacrum | exact | y | 0 | 8 | 21385 | single_pass |  |

## S2

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Acute appendicitis | wrong | n | 11 | 21 | 129133 | finish |  |
| case-02 | breast fibromatosis | Fibromatosis (desmoid tumor) of the breast | exact | y | 12 | 7 | 59093 | finish |  |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Bone Metastasis (Sclerotic) | wrong | n | 10 | 6 | 69949 | finish |  |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic diverticulum | exact | y | 5 | 2 | 44294 | finish |  |
| case-05 | extensive multi vessels coronary artery disease | Severe coronary artery disease (atherosclerosis with extensive calcification) | exact | y | 13 | 13 | 116560 | finish |  |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 9 | 5 | 51455 | finish |  |
| case-07 | patellar tendon rupture | Patellar tendon rupture | exact | y | 14 | 10 | 64963 | finish |  |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Disorder of sexual development (DSD) with associated skeletal anomalies (likely congenital adrenal hyperplasia or chromosomal DSD) | wrong | n | 14 | 7 | 93415 | finish |  |
| case-09 | slipped capital femoral epiphysis scfe | Bilateral slipped capital femoral epiphysis (SCFE) with surgical fixation | exact | y | 9 | 4 | 54928 | finish |  |
| case-10 | spondylolisthesis with follow up | Adjacent segment disease at L4-L5 following L5-S1 fusion | wrong | n | 13 | 8 | 90294 | finish |  |

## S3

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Acute appendicitis | wrong | n | 11 | 9 | 106064 | finish | finalize/0 |
| case-02 | breast fibromatosis | Fibromatosis (Desmoid Tumor) of the breast | exact | y | 12 | 7 | 68276 | finish | finalize/0 |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Localized bladder cancer without evidence of bone metastases | wrong | y | 9 | 5 | 92413 | finish | finalize/0 |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic Diverticulum | exact | y | 5 | 2 | 34392 | finish | finalize/0 |
| case-05 | extensive multi vessels coronary artery disease | Severe multi-vessel coronary artery disease | exact | y | 12 | 12 | 87501 | finish | finalize/0 |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 9 | 5 | 54123 | finish | finalize/0 |
| case-07 | patellar tendon rupture | Complete patellar tendon tear | exact | y | 10 | 6 | 107626 | finish | finalize/0 |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Currarino syndrome | wrong | n | 13 | 8 | 136114 | finish | finalize/0 |
| case-09 | slipped capital femoral epiphysis scfe | Bilateral Slipped Capital Femoral Epiphysis | exact | y | 9 | 4 | 50307 | finish | finalize/0 |
| case-10 | spondylolisthesis with follow up | Lumbosacral transitional vertebra (Castellvi type IIIb) with severe lumbosacral kyphosis, status post posterior spinal fusion | wrong | n | 13 | 8 | 77446 | finish | finalize/0 |

## S4

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Appendiceal intussusception | exact | y | 9 | 6 | 99558 | finish | finalize/0 |
| case-02 | breast fibromatosis | Desmoid-type fibromatosis | exact | y | 14 | 6 | 57006 | finish | finalize/0 |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Thoracic spondylosis | compatible | n | 9 | 3 | 54754 | finish | finalize/0 |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic diverticulum | exact | y | 6 | 2 | 57781 | finish | finalize/0 |
| case-05 | extensive multi vessels coronary artery disease | Severe multivessel coronary artery disease | exact | y | 14 | 6 | 110626 | finish_after_revisit | revisit/1 |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 11 | 5 | 49800 | finish | finalize/0 |
| case-07 | patellar tendon rupture | Prepatellar Bursitis | wrong | y | 16 | 10 | 94198 | finish_after_revisit | revisit/2 |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Classic Bladder Exstrophy | wrong | y | 14 | 6 | 88523 | finish_after_revisit | revisit/2 |
| case-09 | slipped capital femoral epiphysis scfe | Slipped Capital Femoral Epiphysis | exact | y | 9 | 4 | 69904 | finish | finalize/0 |
| case-10 | spondylolisthesis with follow up | Grade III L5-S1 isthmic spondylolisthesis, status post L5-S1 fusion. | exact | y | 14 | 6 | 70700 | finish_after_revisit | revisit/1 |

## S5

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Acute appendicitis | wrong | n | 12 | 22 | 119794 | finish_after_revisit | revisit/1 |
| case-02 | breast fibromatosis | Desmoid-type fibromatosis | exact | y | 12 | 7 | 96058 | finish | finalize/0 |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Bladder cancer | wrong | n | 9 | 5 | 62937 | finish | finalize/0 |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic Diverticulum | exact | y | 5 | 2 | 31266 | finish | finalize/0 |
| case-05 | extensive multi vessels coronary artery disease | Coronary artery disease | exact | y | 12 | 12 | 144808 | finish_after_revisit | revisit/1 |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 9 | 5 | 47047 | finish | finalize/0 |
| case-07 | patellar tendon rupture | Patellar tendinopathy with partial tear | compatible | n | 12 | 14 | 129079 | finish | finalize/0 |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Developmental dysplasia of the hip | wrong | n | 17 | 18 | 139737 | finish_after_revisit | revisit/2 |
| case-09 | slipped capital femoral epiphysis scfe | Slipped Capital Femoral Epiphysis | exact | y | 9 | 4 | 49051 | finish | finalize/0 |
| case-10 | spondylolisthesis with follow up | Isthmic spondylolisthesis at L5-S1 | exact | y | 13 | 8 | 84400 | finish | finalize/0 |
