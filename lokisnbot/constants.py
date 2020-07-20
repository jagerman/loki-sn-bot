
PROOF_AGE_WARNING = 3600 + 360  # 1 hour plus 6 minutes of grace time (uptime proofs can take slightly more than an hour)
PROOF_AGE_REPEAT = 600  # How often to repeat the alert
STAKE_BLOCKS = 720*30 + 20  # Length of a stake (pre-3.0.0)
TESTNET_STAKE_BLOCKS = 720*2 + 20  # Length of a stake (pre-3.0.0)
INFINITE_FROM = 234767  # Block where infinite stakes began
TESTNET_INFINITE_FROM = 1  # Testnet is always infinite
AVERAGE_BLOCK_SECONDS = 120  # Target block time
COIN = 1000000000  # Number of atomic units in 1 coin

# (height,requirement) pairs for an integer math linear approximation of the staking amount which
# started applying in the Loki 5.x hard fork.  This begins at the first height; anything beyond the
# last height uses the last amount, and anything in between two heights uses the weighted average of
# the two bounding heights' values.  (Up to 861024 these are the exact values of the curve; from
# there to 15000 we deviate slightly and just move to 15000 by height 1M).
LINEARIZE_STAKING = (
        (385824,20458380815527),
        (429024, 19332319724305),
        (472224, 18438564443912),
        (515424, 17729190407764),
        (558624, 17166159862153),
        (601824, 16719282221956),
        (645024, 16364595203882),
        (688224, 16083079931076),
        (731424, 15859641110978),
        (774624, 15682297601941),
        (817824, 15541539965538),
        (861024, 15429820555489),
        (1000000, 15000000000000))
