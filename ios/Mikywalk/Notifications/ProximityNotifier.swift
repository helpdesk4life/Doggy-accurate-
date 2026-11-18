import Foundation
import UserNotifications

/// Proximity alerts, one sound per relationship.
///
/// iOS has no notification channels. Android needs three channels because a
/// channel's sound is fixed at creation; on iOS the sound is set per
/// notification, so the three-way split collapses into one `UNNotificationSound`
/// choice and the code gets simpler.
///
/// The two custom tones are the same WAV files the Android app ships. Copy
/// `android/app/src/main/res/raw/friend_chime.wav` and
/// `android/app/src/main/res/raw/disliked_alarm.wav` into the iOS app bundle
/// unchanged — no conversion needed.
///
/// What you lose against Android: there is one app-level notification toggle, so
/// a user cannot mute "disliked" independently of "friend". `UNNotificationCategory`
/// is not a substitute; it groups notifications for action buttons, not sounds.
@MainActor
final class ProximityNotifier {

    private let center = UNUserNotificationCenter.current()

    func requestAuthorization() async {
        _ = try? await center.requestAuthorization(options: [.alert, .sound])
    }

    /// Alert about the walkers that just came inside the warning distance.
    ///
    /// When several relationships are present at once the most urgent one wins:
    /// a dog yours does not like matters more than one it does, and a stranger
    /// matters more than a friend. Otherwise a friend arriving would replace the
    /// warning with a chime, which is the wrong way round.
    func notifyProximity(_ walkers: [NearbyWalker], marked: [String: MarkedDog]) async {
        guard !walkers.isEmpty else { return }

        let grouped = Dictionary(grouping: walkers) { walker in
            marked[walker.id]?.relationship ?? .unknown
        }
        guard let relationship = grouped.keys.min(by: { urgency($0) < urgency($1) }),
              let group = grouped[relationship],
              let closest = group.min(by: { $0.distanceMeters < $1.distanceMeters })
        else { return }

        let content = UNMutableNotificationContent()
        content.title = title(for: relationship, group: group, closest: closest)
        content.body = "\(closest.name) is \(Int(closest.distanceMeters.rounded())) m away."
        content.sound = sound(for: relationship)
        content.categoryIdentifier = "PROXIMITY"

        // A warning about a dog yours does not like is worth breaking through
        // Focus. `.timeSensitive` needs the Time Sensitive Notifications
        // entitlement; without it iOS delivers the notification normally rather
        // than failing, so this is safe to leave in.
        content.interruptionLevel = relationship == .disliked ? .timeSensitive : .active

        let request = UNNotificationRequest(
            identifier: "proximity-\(closest.id)-\(Int(Date().timeIntervalSince1970))",
            content: content,
            trigger: nil
        )
        try? await center.add(request)
    }

    /// Lower is more urgent.
    private func urgency(_ relationship: DogRelationship) -> Int {
        switch relationship {
        case .disliked: return 0
        case .unknown: return 1
        case .friend: return 2
        }
    }

    private func sound(for relationship: DogRelationship) -> UNNotificationSound {
        switch relationship {
        case .disliked:
            return UNNotificationSound(named: UNNotificationSoundName("disliked_alarm.wav"))
        case .friend:
            return UNNotificationSound(named: UNNotificationSoundName("friend_chime.wav"))
        case .unknown:
            return .default
        }
    }

    private func title(
        for relationship: DogRelationship,
        group: [NearbyWalker],
        closest: NearbyWalker
    ) -> String {
        if relationship == .disliked { return "\(closest.name) is nearby" }
        if group.count == 1 { return "\(closest.name) is nearby" }
        return "\(group.count) dogs nearby"
    }
}
