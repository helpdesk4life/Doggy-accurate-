package com.mikywalk.ui.map

import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.mikywalk.R
import com.mikywalk.geo.GeoMath
import com.mikywalk.model.DogRelationship
import com.mikywalk.model.MarkedDog
import com.mikywalk.model.NearbyWalker
import com.mikywalk.model.WalkerLocation
import org.maplibre.android.camera.CameraUpdateFactory
import org.maplibre.android.geometry.LatLng
import org.maplibre.android.maps.MapLibreMap
import org.maplibre.android.maps.MapView
import org.maplibre.android.maps.Style
import org.maplibre.android.style.layers.CircleLayer
import org.maplibre.android.style.layers.FillLayer
import org.maplibre.android.style.layers.LineLayer
import org.maplibre.android.style.layers.PropertyFactory
import org.maplibre.android.style.sources.GeoJsonSource
import org.maplibre.geojson.Feature
import org.maplibre.geojson.FeatureCollection
import org.maplibre.geojson.Point
import org.maplibre.geojson.Polygon

private const val STYLE_URI = "asset://style.json"
private const val SOURCE_SELF = "mikywalk-self"
private const val SOURCE_RADIUS = "mikywalk-radius"
private const val SOURCE_WARNING = "mikywalk-warning"
private const val SOURCE_NEARBY_FRIEND = "mikywalk-nearby-friend"
private const val SOURCE_NEARBY_DISLIKED = "mikywalk-nearby-disliked"
private const val SOURCE_NEARBY_UNKNOWN = "mikywalk-nearby-unknown"
private const val LAYER_RADIUS_FILL = "mikywalk-radius-fill"
private const val LAYER_RADIUS_LINE = "mikywalk-radius-line"
private const val LAYER_WARNING_FILL = "mikywalk-warning-fill"
private const val LAYER_WARNING_LINE = "mikywalk-warning-line"
private const val LAYER_NEARBY_FRIEND = "mikywalk-nearby-friend-layer"
private const val LAYER_NEARBY_DISLIKED = "mikywalk-nearby-disliked-layer"
private const val LAYER_NEARBY_UNKNOWN = "mikywalk-nearby-unknown-layer"
private const val LAYER_SELF = "mikywalk-self-layer"
private const val CAMERA_ZOOM = 14.5

private val SELF_LABEL_COLOR = Color(0xFF1B5E4B)
private val FRIEND_LABEL_COLOR = Color(0xFF1B6E3A)
private val DISLIKED_LABEL_COLOR = Color(0xFFA02020)
private val UNKNOWN_LABEL_COLOR = Color(0xFF8A5A00)

/**
 * A name tag drawn over the map, anchored to a marker's projected screen position.
 *
 * [walker] is null for our own tag. A non-null one makes the tag tappable, which
 * is how a dog gets marked as liked or disliked: the tag is already positioned
 * over the marker, so it is a far more reliable hit target than a MapLibre
 * feature query.
 */
private data class MapLabel(
    val text: String,
    val anchor: Offset,
    val relationship: DogRelationship,
    val isSelf: Boolean,
    val walker: NearbyWalker?,
)

/**
 * A neighbour's marker position, resolved from our own fix plus the bearing and
 * distance the server sent. The server never sends a coordinate.
 */
private data class ResolvedWalker(
    val id: String,
    val name: String,
    val latitude: Double,
    val longitude: Double,
    val relationship: DogRelationship,
    val walker: NearbyWalker,
)

/**
 * MapLibre map hosted in Compose.
 *
 * Four GeoJSON sources drive the markers: the walker's own point, the proximity
 * radius ring, the warning ring, and one source per relationship so each can have
 * its own colour. Updating a source is enough to move the markers, so no
 * annotation objects are allocated per fix.
 *
 * Three sources rather than one with a data-driven colour expression is a
 * deliberate trade: it is more code, but a `match` expression that is subtly
 * wrong fails silently, and this project has already lost time to MapLibre
 * features that do not behave as documented.
 *
 * Name tags are Compose text rather than a MapLibre symbol layer: symbol layers need a
 * glyph server, and the labels stay legible and offline this way. Positions come from
 * [org.maplibre.android.maps.Projection.toScreenLocation] and are refreshed on every
 * camera move.
 */
@Composable
fun MapLibreMap(
    self: WalkerLocation?,
    selfName: String,
    nearby: List<NearbyWalker>,
    marked: Map<String, MarkedDog>,
    radiusMeters: Double,
    warningDistanceMeters: Double,
    recenterSignal: Int,
    onWalkerTapped: (NearbyWalker) -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current

    val mapView = remember {
        MapView(context).apply { onCreate(null) }
    }

    var map by remember { mutableStateOf<MapLibreMap?>(null) }
    var style by remember { mutableStateOf<Style?>(null) }
    var cameraTick by remember { mutableIntStateOf(0) }
    var labels by remember { mutableStateOf<List<MapLabel>>(emptyList()) }
    val centeredSignal = remember { intArrayOf(Int.MIN_VALUE) }

    DisposableEffect(lifecycleOwner, mapView) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_START -> mapView.onStart()
                Lifecycle.Event.ON_RESUME -> mapView.onResume()
                Lifecycle.Event.ON_PAUSE -> mapView.onPause()
                Lifecycle.Event.ON_STOP -> mapView.onStop()
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)

        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            mapView.onPause()
            mapView.onStop()
            mapView.onDestroy()
        }
    }

    LaunchedEffect(mapView) {
        mapView.getMapAsync { loadedMap ->
            loadedMap.uiSettings.isAttributionEnabled = true
            loadedMap.uiSettings.isLogoEnabled = false
            loadedMap.uiSettings.isCompassEnabled = true
            loadedMap.addOnCameraMoveListener { cameraTick++ }
            loadedMap.setStyle(Style.Builder().fromUri(STYLE_URI)) { loadedStyle ->
                installLayers(loadedStyle, context)
                style = loadedStyle
            }
            map = loadedMap
        }
    }

    LaunchedEffect(style, self, radiusMeters, warningDistanceMeters) {
        val loadedStyle = style ?: return@LaunchedEffect
        val me = self

        if (me == null) {
            listOf(SOURCE_SELF, SOURCE_RADIUS, SOURCE_WARNING).forEach { key ->
                loadedStyle.getSourceAs<GeoJsonSource>(key)
                    ?.setGeoJson(FeatureCollection.fromFeatures(emptyList()))
            }
            return@LaunchedEffect
        }

        loadedStyle.getSourceAs<GeoJsonSource>(SOURCE_SELF)?.setGeoJson(
            FeatureCollection.fromFeatures(
                listOf(Feature.fromGeometry(Point.fromLngLat(me.longitude, me.latitude))),
            ),
        )
        loadedStyle.getSourceAs<GeoJsonSource>(SOURCE_RADIUS)
            ?.setGeoJson(ringPolygon(me, radiusMeters))
        loadedStyle.getSourceAs<GeoJsonSource>(SOURCE_WARNING)
            ?.setGeoJson(ringPolygon(me, warningDistanceMeters))
    }

    // The server sends a bearing and a distance, never a position. Resolve them
    // against our own fix once, so the marker source and the name labels agree.
    val resolved = remember(self, nearby, marked) {
        val me = self ?: return@remember emptyList()
        nearby.map { walker ->
            val (latitude, longitude) = GeoMath.destination(
                latitude = me.latitude,
                longitude = me.longitude,
                bearingDegrees = walker.bearingDegrees,
                distanceMeters = walker.distanceMeters,
            )
            ResolvedWalker(
                id = walker.id,
                name = walker.name,
                latitude = latitude,
                longitude = longitude,
                relationship = marked[walker.id]?.relationship ?: DogRelationship.UNKNOWN,
                walker = walker,
            )
        }
    }

    LaunchedEffect(style, resolved) {
        val loadedStyle = style ?: return@LaunchedEffect
        val byRelationship = resolved.groupBy { it.relationship }
        listOf(
            SOURCE_NEARBY_FRIEND to DogRelationship.FRIEND,
            SOURCE_NEARBY_DISLIKED to DogRelationship.DISLIKED,
            SOURCE_NEARBY_UNKNOWN to DogRelationship.UNKNOWN,
        ).forEach { (source, relationship) ->
            val features = byRelationship[relationship].orEmpty().map { walker ->
                Feature.fromGeometry(Point.fromLngLat(walker.longitude, walker.latitude))
            }
            loadedStyle.getSourceAs<GeoJsonSource>(source)
                ?.setGeoJson(FeatureCollection.fromFeatures(features))
        }
    }

    LaunchedEffect(map, self, selfName, resolved, cameraTick) {
        val loadedMap = map ?: return@LaunchedEffect
        val projection = loadedMap.projection
        val next = buildList {
            val me = self
            if (me != null && selfName.isNotBlank()) {
                add(
                    MapLabel(
                        text = selfName,
                        anchor = projection.toScreenLocation(LatLng(me.latitude, me.longitude))
                            .toOffset(),
                        relationship = DogRelationship.UNKNOWN,
                        isSelf = true,
                        walker = null,
                    ),
                )
            }
            resolved.forEach { walker ->
                if (walker.name.isBlank()) return@forEach
                add(
                    MapLabel(
                        text = walker.name,
                        anchor = projection
                            .toScreenLocation(LatLng(walker.latitude, walker.longitude))
                            .toOffset(),
                        relationship = walker.relationship,
                        isSelf = false,
                        walker = walker.walker,
                    ),
                )
            }
        }
        labels = next
    }

    LaunchedEffect(map, self, recenterSignal) {
        val loadedMap = map ?: return@LaunchedEffect
        val me = self ?: return@LaunchedEffect
        if (centeredSignal[0] == recenterSignal) return@LaunchedEffect
        centeredSignal[0] = recenterSignal
        loadedMap.animateCamera(
            CameraUpdateFactory.newLatLngZoom(LatLng(me.latitude, me.longitude), CAMERA_ZOOM),
            600,
        )
    }

    Box(modifier = modifier.clipToBounds()) {
        AndroidView(factory = { mapView }, modifier = Modifier.fillMaxSize())

        labels.forEach { label ->
            val walker = label.walker
            Text(
                text = label.text,
                color = when {
                    label.isSelf -> SELF_LABEL_COLOR
                    label.relationship == DogRelationship.FRIEND -> FRIEND_LABEL_COLOR
                    label.relationship == DogRelationship.DISLIKED -> DISLIKED_LABEL_COLOR
                    else -> UNKNOWN_LABEL_COLOR
                },
                fontSize = if (label.isSelf) 13.sp else 12.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier
                    .graphicsLayer {
                        translationX = label.anchor.x - size.width / 2f
                        translationY = label.anchor.y + 12.dp.toPx()
                    }
                    .background(
                        color = Color.White.copy(alpha = 0.88f),
                        shape = RoundedCornerShape(6.dp),
                    )
                    .then(
                        if (walker != null) {
                            Modifier.clickable { onWalkerTapped(walker) }
                        } else {
                            Modifier
                        },
                    )
                    .padding(horizontal = 5.dp, vertical = 1.dp),
            )
        }
    }
}

private fun android.graphics.PointF.toOffset() = Offset(x, y)

private fun installLayers(style: Style, context: Context) {
    style.addSource(GeoJsonSource(SOURCE_RADIUS))
    style.addSource(GeoJsonSource(SOURCE_WARNING))
    style.addSource(GeoJsonSource(SOURCE_NEARBY_FRIEND))
    style.addSource(GeoJsonSource(SOURCE_NEARBY_DISLIKED))
    style.addSource(GeoJsonSource(SOURCE_NEARBY_UNKNOWN))
    style.addSource(GeoJsonSource(SOURCE_SELF))

    style.addLayer(
        FillLayer(LAYER_RADIUS_FILL, SOURCE_RADIUS).withProperties(
            PropertyFactory.fillColor("rgba(27, 94, 75, 0.15)"),
            PropertyFactory.fillOutlineColor("rgba(27, 94, 75, 0.30)"),
        ),
    )
    style.addLayer(
        LineLayer(LAYER_RADIUS_LINE, SOURCE_RADIUS).withProperties(
            PropertyFactory.lineColor("#1B5E4B"),
            PropertyFactory.lineWidth(2f),
        ),
    )
    style.addLayer(
        FillLayer(LAYER_WARNING_FILL, SOURCE_WARNING).withProperties(
            PropertyFactory.fillColor("rgba(224, 160, 48, 0.18)"),
            PropertyFactory.fillOutlineColor("rgba(224, 160, 48, 0.35)"),
        ),
    )
    style.addLayer(
        LineLayer(LAYER_WARNING_LINE, SOURCE_WARNING).withProperties(
            PropertyFactory.lineColor("#E0A030"),
            PropertyFactory.lineWidth(2f),
        ),
    )
    style.addLayer(
        CircleLayer(LAYER_NEARBY_UNKNOWN, SOURCE_NEARBY_UNKNOWN).withProperties(
            PropertyFactory.circleRadius(9f),
            PropertyFactory.circleColor(ContextCompat.getColor(context, R.color.marker_unknown)),
            PropertyFactory.circleStrokeWidth(3f),
            PropertyFactory.circleStrokeColor("#FFFFFF"),
        ),
    )
    style.addLayer(
        CircleLayer(LAYER_NEARBY_FRIEND, SOURCE_NEARBY_FRIEND).withProperties(
            PropertyFactory.circleRadius(9f),
            PropertyFactory.circleColor(ContextCompat.getColor(context, R.color.marker_friend)),
            PropertyFactory.circleStrokeWidth(3f),
            PropertyFactory.circleStrokeColor("#FFFFFF"),
        ),
    )
    style.addLayer(
        CircleLayer(LAYER_NEARBY_DISLIKED, SOURCE_NEARBY_DISLIKED).withProperties(
            PropertyFactory.circleRadius(9f),
            PropertyFactory.circleColor(ContextCompat.getColor(context, R.color.marker_disliked)),
            PropertyFactory.circleStrokeWidth(3f),
            PropertyFactory.circleStrokeColor("#FFFFFF"),
        ),
    )
    style.addLayer(
        CircleLayer(LAYER_SELF, SOURCE_SELF).withProperties(
            PropertyFactory.circleRadius(8f),
            PropertyFactory.circleColor("#1B5E4B"),
            PropertyFactory.circleStrokeWidth(3f),
            PropertyFactory.circleStrokeColor("#FFFFFF"),
        ),
    )
}

/** Closed polygon approximating a circle of [radiusMeters] around [center]. */
private fun ringPolygon(center: WalkerLocation, radiusMeters: Double): Polygon {
    val ring = GeoMath.circle(center.latitude, center.longitude, radiusMeters)
    val closed = ring + ring.first()
    return Polygon.fromLngLats(
        listOf(closed.map { (latitude, longitude) -> Point.fromLngLat(longitude, latitude) }),
    )
}
