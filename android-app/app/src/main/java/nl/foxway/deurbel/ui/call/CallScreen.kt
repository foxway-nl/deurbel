package nl.foxway.deurbel.ui.call

import android.graphics.Bitmap
import androidx.compose.animation.*
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import nl.foxway.deurbel.network.LiveStreamController

/**
 * Fullscreen Incoming & Active Call Screen (Instant RAM Live Stream).
 *
 * Requirements:
 *  - 100% Shared LiveStreamController across RINGING and ANSWERED states.
 *  - Pressing "Opnemen" NEVER restarts video, NEVER shows a loading spinner.
 *  - 4:3 aspect ratio native fit with toggle to fill.
 *  - Instant single-touch reaction on buttons (<10ms).
 */
@Composable
fun CallScreen(
    callId: String,
    snapshotUrl: String,
    isActive: Boolean,
    isSilenced: Boolean = false,
    answeredByElsewhere: String?,
    isMicMuted: Boolean,
    isSpeakerOn: Boolean,
    onAnswer: () -> Unit,
    onSilence: () -> Unit,
    onHangup: () -> Unit,
    onToggleMic: () -> Unit,
    onToggleSpeaker: () -> Unit
) {
    val context = LocalContext.current
    val liveStream = remember { LiveStreamController.getInstance(context) }
    val frame by liveStream.currentBitmap.collectAsState()
    val isConnected by liveStream.isConnected.collectAsState()

    var isFitMode by remember { mutableStateOf(true) }

    LaunchedEffect(Unit) {
        liveStream.start()
    }

    // Automatically manage audio mute: muted when ringing (so beltoon is clear), unmuted when answered or speaker on
    LaunchedEffect(isActive, isSpeakerOn) {
        liveStream.setAudioMuted(!isActive || !isSpeakerOn)
        if (isActive && isSpeakerOn) {
            try {
                val audioManager = context.getSystemService(android.content.Context.AUDIO_SERVICE) as? android.media.AudioManager
                audioManager?.mode = android.media.AudioManager.MODE_IN_COMMUNICATION
                audioManager?.isSpeakerphoneOn = true
            } catch (_: Exception) {}
        }
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black)
    ) {
        // ── 1. Live Video Stream Container (4:3 Native Aspect Ratio) ──
        Box(
            modifier = Modifier
                .fillMaxSize(),
            contentAlignment = Alignment.Center
        ) {
            Box(
                modifier = if (isFitMode) {
                    Modifier
                        .fillMaxWidth()
                        .aspectRatio(4f / 3f)
                        .clipToBounds()
                } else {
                    Modifier.fillMaxSize()
                },
                contentAlignment = Alignment.Center
            ) {
                if (frame != null) {
                    Image(
                        bitmap = frame!!.asImageBitmap(),
                        contentDescription = "Live beeld voordeur",
                        contentScale = if (isFitMode) ContentScale.Fit else ContentScale.Crop,
                        modifier = Modifier.fillMaxSize()
                    )
                } else if (snapshotUrl.isNotBlank()) {
                    AsyncImage(
                        model = snapshotUrl,
                        contentDescription = "Snapshot voordeur",
                        contentScale = if (isFitMode) ContentScale.Fit else ContentScale.Crop,
                        modifier = Modifier.fillMaxSize()
                    )
                } else {
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .background(Color(0xFF111827)),
                        contentAlignment = Alignment.Center
                    ) {
                        CircularProgressIndicator(
                            color = Color(0xFF10B981),
                            modifier = Modifier.size(36.dp)
                        )
                    }
                }
            }
        }

        // ── 2. Gradient Overlays for Readability ──
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(
                    Brush.verticalGradient(
                        colors = listOf(
                            Color.Black.copy(alpha = 0.65f),
                            Color.Transparent,
                            Color.Black.copy(alpha = 0.85f)
                        )
                    )
                )
        )

        // ── 3. Top Bar: Status Badge + Fit/Fill Toggle ──
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .statusBarsPadding()
                .padding(top = 16.dp, start = 20.dp, end = 20.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Surface(
                color = Color(0xCC1E293B),
                shape = RoundedCornerShape(20.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155))
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Box(
                        modifier = Modifier
                            .size(10.dp)
                            .background(
                                if (isActive) Color(0xFF10B981)
                                else if (isSilenced) Color(0xFFF59E0B)
                                else Color(0xFFEF4444),
                                CircleShape
                            )
                    )
                    Text(
                        text = if (isActive) "Actief gesprek • Voordeur"
                        else if (isSilenced) "Stil meekijken • Voordeur"
                        else "Aanbellen • Live beeld",
                        style = MaterialTheme.typography.labelMedium,
                        fontWeight = FontWeight.SemiBold,
                        color = Color.White
                    )
                }
            }

            Surface(
                color = Color(0xCC1E293B),
                shape = RoundedCornerShape(20.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155)),
                modifier = Modifier.clickable { isFitMode = !isFitMode }
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    Icon(
                        imageVector = if (isFitMode) Icons.Default.AspectRatio else Icons.Default.FitScreen,
                        contentDescription = "Beeldverhouding",
                        tint = Color(0xFF38BDF8),
                        modifier = Modifier.size(16.dp)
                    )
                    Text(
                        text = if (isFitMode) "4:3 Fit" else "Fill",
                        style = MaterialTheme.typography.labelSmall,
                        color = Color.White
                    )
                }
            }
        }

        // Active microphone banner
        if (isActive && !isMicMuted) {
            Surface(
                color = Color(0xFFDC2626),
                shape = RoundedCornerShape(20.dp),
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .statusBarsPadding()
                    .padding(top = 70.dp)
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Box(modifier = Modifier.size(8.dp).background(Color.White, CircleShape))
                    Text(
                        text = "MICROFOON ACTIEF — DEURBEL HOORT JOU",
                        color = Color.White,
                        fontWeight = FontWeight.Bold,
                        style = MaterialTheme.typography.labelSmall
                    )
                }
            }
        }

        // ── 4. Multi-User Claim Banner ──
        AnimatedVisibility(
            visible = answeredByElsewhere != null,
            enter = fadeIn(),
            exit = fadeOut(),
            modifier = Modifier
                .align(Alignment.Center)
                .padding(24.dp)
        ) {
            Surface(
                color = Color(0xFF0F172A),
                shape = RoundedCornerShape(16.dp),
                border = androidx.compose.foundation.BorderStroke(2.dp, Color(0xFF38BDF8)),
                shadowElevation = 8.dp
            ) {
                Column(
                    modifier = Modifier.padding(24.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    Icon(
                        imageVector = Icons.Default.CheckCircle,
                        contentDescription = null,
                        tint = Color(0xFF38BDF8),
                        modifier = Modifier.size(48.dp)
                    )
                    Text(
                        text = "Beantwoord door $answeredByElsewhere",
                        style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold),
                        color = Color.White,
                        textAlign = TextAlign.Center
                    )
                    Text(
                        text = "Deze oproep wordt gesloten…",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Color(0xFF94A3B8)
                    )
                }
            }
        }

        // ── 5. Bottom Controls (Incoming 3-Buttons vs Active Controls) ──
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(bottom = 28.dp, start = 16.dp, end = 16.dp),
            contentAlignment = Alignment.Center
        ) {
            if (!isActive && answeredByElsewhere == null) {
                // Incoming Call: 3 Equal 64.dp Buttons
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp),
                    horizontalArrangement = Arrangement.SpaceEvenly,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    // 1. 🔴 Ophangen / Weigeren
                    CallActionButton(
                        icon = Icons.Default.CallEnd,
                        label = "Ophangen",
                        backgroundColor = Color(0xFFE53935),
                        iconTint = Color.White,
                        onClick = onHangup
                    )

                    // 2. 🔕 Stil / Negeren
                    CallActionButton(
                        icon = Icons.Default.NotificationsOff,
                        label = "Stil meekijken",
                        backgroundColor = Color(0xFF37474F),
                        iconTint = Color.White,
                        onClick = onSilence
                    )

                    // 3. 🟢 Opnemen
                    CallActionButton(
                        icon = Icons.Default.Call,
                        label = "Opnemen",
                        backgroundColor = Color(0xFF10B981),
                        iconTint = Color.White,
                        onClick = onAnswer
                    )
                }
            } else if (isActive && answeredByElsewhere == null) {
                // Active In-Call Floating Control Bar
                Surface(
                    color = Color(0xCC0F172A),
                    shape = RoundedCornerShape(36.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF334155))
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 24.dp, vertical = 12.dp),
                        horizontalArrangement = Arrangement.spacedBy(24.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        // Mic toggle (Two-way voice to outdoor speaker)
                        IconButton(
                            onClick = onToggleMic,
                            modifier = Modifier
                                .size(52.dp)
                                .background(
                                    if (!isMicMuted) Color(0xFFDC2626) else Color(0x33FFFFFF),
                                    CircleShape
                                )
                        ) {
                            Icon(
                                imageVector = if (isMicMuted) Icons.Default.MicOff else Icons.Default.Mic,
                                contentDescription = "Microfoon",
                                tint = Color.White,
                                modifier = Modifier.size(26.dp)
                            )
                        }

                        // 🔴 Ophangen
                        IconButton(
                            onClick = onHangup,
                            modifier = Modifier
                                .size(64.dp)
                                .background(Color(0xFFE53935), CircleShape)
                        ) {
                            Icon(
                                imageVector = Icons.Default.CallEnd,
                                contentDescription = "Ophangen",
                                tint = Color.White,
                                modifier = Modifier.size(32.dp)
                            )
                        }

                        // Speaker toggle
                        IconButton(
                            onClick = onToggleSpeaker,
                            modifier = Modifier
                                .size(52.dp)
                                .background(Color(0x33FFFFFF), CircleShape)
                        ) {
                            Icon(
                                imageVector = if (isSpeakerOn) Icons.Default.VolumeUp else Icons.Default.VolumeOff,
                                contentDescription = "Luidspreker",
                                tint = Color.White,
                                modifier = Modifier.size(26.dp)
                            )
                        }
                    }
                }
            }
        }
    }
}

/**
 * 64.dp Circular Action Button for incoming calls with instant touch response (<10ms).
 */
@Composable
private fun CallActionButton(
    icon: ImageVector,
    label: String,
    backgroundColor: Color,
    iconTint: Color,
    onClick: () -> Unit
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Surface(
            modifier = Modifier
                .size(64.dp)
                .clip(CircleShape)
                .clickable(onClick = onClick),
            shape = CircleShape,
            color = backgroundColor,
            shadowElevation = 4.dp
        ) {
            Box(contentAlignment = Alignment.Center) {
                Icon(
                    imageVector = icon,
                    contentDescription = label,
                    tint = iconTint,
                    modifier = Modifier.size(30.dp)
                )
            }
        }
        Text(
            text = label,
            color = Color.White,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Medium
        )
    }
}
